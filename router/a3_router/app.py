"""HTTP surface of the router: a Codex Responses endpoint in front of two upstreams.

Run: ``uvicorn a3_router.app:app --host 127.0.0.1 --port 8787`` and start Hermes with
``HERMES_CODEX_BASE_URL=http://127.0.0.1:8787``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import __version__
from .policy import Decision, Settings, State, decide, extract_features

HERMES_VERSION = "0.21.5"
# Hop-by-hop or recomputed headers that must not be copied to the upstream.
DROP_REQUEST_HEADERS = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding"}
# Reasoning and compaction items carry Codex-encrypted blobs only the cloud can read.
CLOUD_ONLY_ITEM_TYPES = {"reasoning", "compaction"}
LOCAL_DROP_FIELDS = ("include", "prompt_cache_key", "service_tier", "text", "client_metadata")


@dataclass(frozen=True)
class Config:
    cloud_base_url: str = "https://chatgpt.com/backend-api/codex"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_proxy: str | None = None          # socks5h://127.0.0.1:1055 in the cloud container
    local_model: str = "gemma4:e4b"
    local_reasoning_effort: str | None = "none"
    state_dir: Path = Path("state")
    rate_limit_cooldown_s: int = 600
    health_ttl_s: int = 60
    local_max_tokens: int = 40_000
    local_agentic_max_tokens: int = 8_000

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ.get
        return cls(
            cloud_base_url=env("A3_CLOUD_BASE_URL", cls.cloud_base_url).rstrip("/"),
            ollama_base_url=env("A3_OLLAMA_BASE_URL", cls.ollama_base_url).rstrip("/"),
            ollama_proxy=env("A3_OLLAMA_PROXY") or None,
            local_model=env("A3_LOCAL_MODEL", cls.local_model),
            local_reasoning_effort=env("A3_LOCAL_REASONING_EFFORT", "none") or None,
            state_dir=Path(env("A3_STATE_DIR", "state")),
            rate_limit_cooldown_s=int(env("A3_RATE_LIMIT_COOLDOWN_S", "600")),
            health_ttl_s=int(env("A3_HEALTH_TTL_S", "60")),
            local_max_tokens=int(env("A3_LOCAL_MAX_TOKENS", "40000")),
            local_agentic_max_tokens=int(env("A3_LOCAL_AGENTIC_MAX_TOKENS", "8000")),
        )

    @property
    def simulate_flag(self) -> Path:
        return self.state_dir / "flags" / "simulate_cloud_429"


class RouterState:
    """Mutable runtime state: rate-limit cooldown and cached local health."""

    def __init__(self, config: Config, local_client: httpx.AsyncClient):
        self.config = config
        self.local_client = local_client
        self.cooldown_until = 0.0
        self._health: tuple[float, bool, str] = (0.0, False, "unchecked")

    def note_cloud_429(self) -> None:
        self.cooldown_until = time.time() + self.config.rate_limit_cooldown_s

    def rate_limit(self) -> tuple[bool, bool]:
        simulated = self.config.simulate_flag.exists()
        return simulated or time.time() < self.cooldown_until, simulated

    async def local_health(self, force: bool = False) -> tuple[bool, str]:
        checked_at, up, detail = self._health
        if not force and time.time() - checked_at < self.config.health_ttl_s:
            return up, detail
        try:
            resp = await self.local_client.get("/api/tags", timeout=httpx.Timeout(3.0))
            names = {m.get("name") for m in resp.json().get("models", [])} if resp.status_code == 200 else set()
            if resp.status_code != 200:
                up, detail = False, f"http_{resp.status_code}"
            elif self.config.local_model not in names:
                up, detail = False, "model_missing"
            else:
                up, detail = True, "ok"
        except httpx.HTTPError as exc:
            up, detail = False, f"transport:{type(exc).__name__}"
        self._health = (time.time(), up, detail)
        return up, detail

    def mark_local_down(self, detail: str) -> None:
        self._health = (time.time(), False, detail)


class RoutingLog:
    """Append-only JSONL, one file per UTC day, mirrored to stdout for Cloud Logging."""

    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)

    def write(self, record: dict[str, Any]) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **record}
        line = json.dumps(record, ensure_ascii=False)
        path = self.directory / f"{record['ts'][:10]}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        print(line, file=sys.stdout, flush=True)


# ── request transforms ────────────────────────────────────────────────────────────────────

def jwt_account_headers(authorization: str) -> dict[str, str]:
    """Same workspace headers Hermes derives for the official endpoint (agent/codex_headers.py).

    Hermes only adds them when it talks to chatgpt.com directly, so the router restores them
    from the bearer token it is forwarding. The token itself is never logged.
    """
    token = authorization.removeprefix("Bearer ").strip()
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, json.JSONDecodeError):
        return {}
    auth = payload.get("https://api.openai.com/auth", {}) if isinstance(payload, dict) else {}
    headers = {}
    if isinstance(auth.get("chatgpt_account_id"), str):
        headers["ChatGPT-Account-ID"] = auth["chatgpt_account_id"]
    residency = auth.get("chatgpt_data_residency") or auth.get("chatgpt_compute_residency")
    if isinstance(residency, str) and residency.strip():
        headers["x-openai-internal-codex-residency"] = residency.strip()
    return headers


def cloud_headers(incoming: dict[str, str]) -> dict[str, str]:
    headers = {k: v for k, v in incoming.items() if k.lower() not in DROP_REQUEST_HEADERS}
    lowered = {k.lower() for k in headers}
    for name, value in jwt_account_headers(incoming.get("authorization", "")).items():
        if name.lower() not in lowered:
            headers[name] = value
    # The router forwards Hermes' own requests, so it keeps Hermes' harness identity.
    headers = {k: v for k, v in headers.items() if k.lower() not in {"user-agent", "originator"}}
    headers["User-Agent"] = f"HermesAgent/{HERMES_VERSION} (a3-router/{__version__})"
    headers["originator"] = "hermes-agent"
    return headers


def to_local_body(body: dict[str, Any], config: Config) -> dict[str, Any]:
    local = {k: v for k, v in body.items() if k not in LOCAL_DROP_FIELDS}
    local["model"] = config.local_model
    if isinstance(local.get("tools"), list):
        # Hermes swaps its own web_search for the Codex built-in {"type": "web_search"}. Ollama
        # treats built-in tools as its cloud features (which need an Ollama account and would
        # send the query off the laptop), so the local path keeps plain function tools only.
        local["tools"] = [t for t in local["tools"] if isinstance(t, dict) and t.get("type") == "function"]
        if not local["tools"]:
            local.pop("tools")
            local.pop("tool_choice", None)
    if isinstance(local.get("input"), list):
        local["input"] = [i for i in local["input"]
                          if not (isinstance(i, dict) and i.get("type") in CLOUD_ONLY_ITEM_TYPES)]
    if config.local_reasoning_effort:
        local["reasoning"] = {"effort": config.local_reasoning_effort}
    else:
        local.pop("reasoning", None)
    return local


# ── synthesized replies ───────────────────────────────────────────────────────────────────

def hold_message(decision: Decision) -> str:
    if decision.reason == "privacy_high_too_long":
        return ("This request contains private information and is too long for the local model. "
                "I held it instead of sending it to the cloud. Please shorten it or split it up.")
    return ("The local model (laptop) is unreachable right now, so I held this request instead of sending "
            "private information to the cloud. I'll handle it once the laptop is back online.")


def synth_response(text: str, model: str, stream: bool) -> Response:
    resp_id, item_id = f"resp_{uuid.uuid4().hex}", f"msg_{uuid.uuid4().hex}"
    item = {"type": "message", "id": item_id, "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}
    final = {"id": resp_id, "object": "response", "created_at": int(time.time()), "status": "completed",
             "model": model, "output": [item],
             "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}
    if not stream:
        return JSONResponse(final)
    events = [
        {"type": "response.created", "response": {**final, "status": "in_progress", "output": []}},
        {"type": "response.output_item.added", "output_index": 0,
         "item": {**item, "status": "in_progress", "content": []}},
        {"type": "response.output_text.delta", "item_id": item_id, "output_index": 0, "content_index": 0,
         "delta": text},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": final},
    ]
    payload = "".join(f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events)
    return Response(payload, media_type="text/event-stream")


# ── application ───────────────────────────────────────────────────────────────────────────

def create_app(config: Config | None = None, *, cloud_transport: httpx.AsyncBaseTransport | None = None,
               local_transport: httpx.AsyncBaseTransport | None = None) -> Starlette:
    config = config or Config.from_env()
    long_timeout = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)
    cloud = httpx.AsyncClient(base_url=config.cloud_base_url, timeout=long_timeout, transport=cloud_transport)
    local = httpx.AsyncClient(base_url=config.ollama_base_url, timeout=long_timeout, transport=local_transport,
                              proxy=None if local_transport else config.ollama_proxy,
                              headers={"Authorization": "Bearer ollama"})
    state = RouterState(config, local)
    log = RoutingLog(config.state_dir / "logs" / "routing")
    settings = Settings(local_max_tokens=config.local_max_tokens,
                        local_agentic_max_tokens=config.local_agentic_max_tokens)

    async def relay(upstream: httpx.Response, record: dict[str, Any], started: float) -> AsyncIterator[bytes]:
        """Pass bytes through while watching for failures reported inside a 200 response.

        SSE streams end with a ``response.completed`` event; a non-streaming JSON body
        (``stream: false``) carries ``"status": "completed"`` instead.
        """
        outcome, tail = "incomplete", b""
        is_json = "json" in upstream.headers.get("content-type", "")
        body = bytearray()
        try:
            # Decoded bytes: httpx asks for gzip itself, and the relayed response carries no
            # Content-Encoding header.
            async for chunk in upstream.aiter_bytes():
                window = tail + chunk
                if b'"type":"response.completed"' in window or b'"type": "response.completed"' in window:
                    outcome = "ok"
                if (b'"type":"response.failed"' in window or b'"type": "response.failed"' in window
                        or b'"type":"error"' in window or b'"type": "error"' in window):
                    outcome = "stream_error"
                tail = window[-64:]
                if is_json:
                    body += chunk
                yield chunk
            if is_json and outcome == "incomplete":
                try:
                    status = json.loads(bytes(body)).get("status")
                    outcome = "ok" if status == "completed" else f"status_{status}"
                except (ValueError, AttributeError):
                    outcome = "bad_json"
        except httpx.HTTPError as exc:
            outcome = f"transport_error:{type(exc).__name__}"
            raise
        except (GeneratorExit, asyncio.CancelledError):
            # Hermes stopped reading early (it does this for short auxiliary calls such as
            # session titles); the upstream answer itself did not fail.
            if outcome == "incomplete":
                outcome = "client_closed"
            raise
        finally:
            await upstream.aclose()
            log.write({**record, "outcome": outcome, "latency_ms": int((time.monotonic() - started) * 1000)})

    async def send(client: httpx.AsyncClient, path: str, body: dict[str, Any],
                   headers: dict[str, str]) -> httpx.Response:
        request = client.build_request("POST", path, json=body, headers=headers)
        return await client.send(request, stream=True)

    async def forward(route: str, body: dict[str, Any], request: Request) -> httpx.Response:
        if route == "cloud":
            return await send(cloud, "/responses", body, cloud_headers(dict(request.headers)))
        return await send(local, "/v1/responses", to_local_body(body, config), {"Content-Type": "application/json"})

    async def responses(request: Request) -> Response:
        started = time.monotonic()
        body = await request.json()
        stream = body.get("stream") is True
        feats = extract_features(body)
        local_up, local_detail = await state.local_health()
        rate_limited, simulated = state.rate_limit()
        decision = decide(feats, State(local_up=local_up, rate_limited=rate_limited,
                                       rate_limit_simulated=simulated), settings)
        record: dict[str, Any] = {"req": f"r-{uuid.uuid4().hex[:8]}", "requested_model": body.get("model"),
                                  **decision.log_fields(), "local_detail": local_detail, "fallback_from": None}

        if decision.route == "hold":
            log.write({**record, "model": None, "outcome": "held", "latency_ms": 0})
            return synth_response(hold_message(decision), "a3-router/hold", stream)

        route = decision.route
        try:
            upstream = await forward(route, body, request)
        except httpx.HTTPError as exc:
            upstream = None
            failure = f"transport_error:{type(exc).__name__}"
            if route == "local":
                state.mark_local_down(failure)
        else:
            failure = None if upstream.status_code < 400 else f"http_{upstream.status_code}"
            if route == "cloud" and upstream.status_code == 429:
                state.note_cloud_429()

        # A failed cloud turn gets one local retry when the local model can hold the prompt.
        # Local failures are never retried in the cloud: that could leak private work.
        cloud_429 = upstream is not None and upstream.status_code == 429
        fits_local = feats.est_tokens <= settings.local_max_tokens
        if failure and route == "cloud" and local_up and fits_local and (decision.fallback_allowed or cloud_429):
            if upstream is not None:
                await upstream.aclose()
            record.update(fallback_from="cloud", first_failure=failure)
            route = "local"
            try:
                upstream = await forward(route, body, request)
                failure = None if upstream.status_code < 400 else f"http_{upstream.status_code}"
            except httpx.HTTPError as exc:
                upstream, failure = None, f"transport_error:{type(exc).__name__}"
                state.mark_local_down(failure)

        record["route"] = route
        record["model"] = config.local_model if route == "local" else body.get("model")
        if upstream is None:
            log.write({**record, "outcome": failure, "latency_ms": int((time.monotonic() - started) * 1000)})
            return JSONResponse({"error": {"message": f"a3-router: {route} upstream unavailable ({failure})",
                                           "type": "upstream_unavailable"}}, status_code=502)
        if failure:
            content = await upstream.aread()
            await upstream.aclose()
            log.write({**record, "outcome": failure, "latency_ms": int((time.monotonic() - started) * 1000)})
            return Response(content, status_code=upstream.status_code,
                            media_type=upstream.headers.get("content-type", "application/json"))
        return StreamingResponse(relay(upstream, record, started), status_code=upstream.status_code,
                                 media_type=upstream.headers.get("content-type", "text/event-stream"))

    async def passthrough(request: Request) -> Response:
        """Everything else Hermes asks the Codex backend for (model catalog, usage) goes to the cloud."""
        path = "/" + request.path_params["path"]
        upstream = await cloud.request(request.method, path, params=request.query_params,
                                       content=await request.body(), headers=cloud_headers(dict(request.headers)))
        return Response(upstream.content, status_code=upstream.status_code,
                        media_type=upstream.headers.get("content-type"))

    async def health(_: Request) -> Response:
        local_up, detail = await state.local_health(force=True)
        rate_limited, simulated = state.rate_limit()
        return JSONResponse({"router": __version__, "local_model": config.local_model, "local_up": local_up,
                             "local_detail": detail, "rate_limited": rate_limited, "simulated": simulated})

    async def ratelimit(request: Request) -> Response:
        mode = request.path_params["mode"]
        flag = config.simulate_flag
        if mode == "on":
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.touch()
        elif mode == "off":
            flag.unlink(missing_ok=True)
        else:
            return JSONResponse({"error": "use on or off"}, status_code=400)
        log.write({"event": "simulate_cloud_429", "mode": mode})
        return JSONResponse({"simulated": flag.exists()})

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette):
        yield
        await cloud.aclose()
        await local.aclose()

    routes = [
        Route("/router/health", health),
        Route("/router/ratelimit/{mode}", ratelimit, methods=["POST"]),
        Route("/responses", responses, methods=["POST"]),
        Route("/v1/responses", responses, methods=["POST"]),
        Route("/{path:path}", passthrough, methods=["GET", "POST"]),
    ]
    return Starlette(routes=routes, lifespan=lifespan)


def __getattr__(name: str):
    # ``uvicorn a3_router.app:app`` builds the app lazily so tests can import this module
    # without reading the environment.
    if name == "app":
        return create_app()
    raise AttributeError(name)
