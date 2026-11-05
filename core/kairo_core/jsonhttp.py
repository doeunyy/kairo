"""Small JSON-over-HTTPS client shared by the Notion and Discord adapters.

Tests swap ``transport`` for a function that answers requests in memory.
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

USER_AGENT = "Kairo (https://github.com/doeunyy/kairo, 0.1)"


@dataclass(frozen=True)
class RawResponse:
    status: int
    headers: dict[str, str]
    body: bytes


Transport = Callable[[urllib.request.Request, float], RawResponse]


class HttpError(RuntimeError):
    def __init__(self, status: int, method: str, path: str, body: Any):
        # The path and body are safe to log: tokens only ever travel in headers.
        detail = body.get("code") or body.get("message") if isinstance(body, dict) else str(body)[:200]
        super().__init__(f"{method} {path} -> HTTP {status} ({detail})")
        self.status, self.body = status, body


def urllib_transport(request: urllib.request.Request, timeout: float) -> RawResponse:
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as resp:
            return RawResponse(resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read())
    except urllib.error.HTTPError as err:
        return RawResponse(err.code, {k.lower(): v for k, v in err.headers.items()}, err.read())


class JsonClient:
    def __init__(self, base_url: str, headers: dict[str, str], transport: Transport | None = None,
                 timeout: float = 20.0, max_retries: int = 3, sleep: Callable[[float], None] = time.sleep):
        self.base_url = base_url.rstrip("/")
        self.headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json", **headers}
        self.transport = transport or urllib_transport
        self.timeout, self.max_retries, self.sleep = timeout, max_retries, sleep

    def request(self, method: str, path: str, body: Any = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=self.headers)
            resp = self.transport(req, self.timeout)
            payload = json.loads(resp.body) if resp.body else None
            if resp.status == 429 and attempt < self.max_retries:
                # Notion sends Retry-After in seconds; Discord also puts retry_after in the body.
                wait = float(resp.headers.get("retry-after") or (payload or {}).get("retry_after") or 1)
                self.sleep(min(wait, 30.0))
                continue
            if resp.status >= 500 and attempt < self.max_retries:
                self.sleep(2.0 ** attempt)
                continue
            if resp.status >= 400:
                raise HttpError(resp.status, method, path, payload)
            return payload
        raise AssertionError("unreachable")
