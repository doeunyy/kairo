"""Router behaviour against mocked Codex and Ollama upstreams."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from a3_router.app import Config, create_app, cloud_headers, to_local_body
from a3_router.policy import Settings, State, decide, extract_features

LOCAL_MODEL = "gemma4:e4b"
COMPLETED_SSE = (b'event: response.completed\ndata: {"type":"response.completed","response":'
                 b'{"status":"completed","output":[]}}\n\n')
FAILED_SSE = b'event: response.failed\ndata: {"type":"response.failed","response":{"status":"failed"}}\n\n'


def fake_jwt(account_id: str = "acct-test") -> str:
    def enc(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'none'})}.{enc({'https://api.openai.com/auth': {'chatgpt_account_id': account_id}})}.sig"


class Upstream:
    """Records requests and answers with a scripted status/body."""

    def __init__(self, status: int = 200, body: bytes = COMPLETED_SSE, models: list[str] | None = None,
                 down: bool = False, content_type: str = "text/event-stream"):
        self.status, self.body, self.down, self.content_type = status, body, down, content_type
        self.models = [LOCAL_MODEL] if models is None else models
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("tailnet unreachable", request=request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in self.models]})
        self.calls.append(request)
        return httpx.Response(self.status, content=self.body, headers={"content-type": self.content_type})

    def posted(self) -> list[dict]:
        return [json.loads(r.content) for r in self.calls]


def make(tmp_path: Path, cloud: Upstream, local: Upstream) -> TestClient:
    config = Config(local_model=LOCAL_MODEL, state_dir=tmp_path)
    app = create_app(config, cloud_transport=httpx.MockTransport(cloud), local_transport=httpx.MockTransport(local))
    return TestClient(app)


def request_body(text: str, *, history: list | None = None, instructions: str = "You are Hermes.") -> dict:
    return {"model": "gpt-5.6-sol", "instructions": instructions, "store": False, "stream": True,
            "input": [*(history or []), {"role": "user", "content": [{"type": "input_text", "text": text}]}],
            "include": ["reasoning.encrypted_content"], "prompt_cache_key": "k"}


def routing_log(tmp_path: Path) -> list[dict]:
    files = sorted((tmp_path / "logs" / "routing").glob("*.jsonl"))
    return [json.loads(line) for f in files for line in f.read_text().splitlines()]


AUTH = {"Authorization": f"Bearer {fake_jwt()}"}


# ── policy ─────────────────────────────────────────────────────────────────────────────

def test_tag_overrides_word_lists():
    feats = extract_features(request_body("<!--route task=jd_compare reasoning=high-->\n연봉 얘기 아님"))
    assert (feats.task, feats.reasoning, feats.privacy, feats.tagged) == ("jd_compare", "high", "normal", True)


def test_untagged_privacy_and_reasoning_terms():
    assert extract_features(request_body("내 희망 연봉 메모 보여줘")).privacy == "high"
    assert extract_features(request_body("두 JD를 비교해줘")).reasoning == "high"
    assert extract_features(request_body("내일 면접 몇 시야?")).simple is True


@pytest.mark.parametrize("feats_kw,state_kw,route,reason", [
    ({"privacy": "high"}, {"local_up": True, "rate_limited": False}, "local", "privacy_high"),
    ({"privacy": "high"}, {"local_up": False, "rate_limited": False}, "hold", "privacy_high_local_unavailable"),
    ({"privacy": "high", "est_tokens": 90_000}, {"local_up": True, "rate_limited": False}, "hold",
     "privacy_high_too_long"),
    ({"simple": True}, {"local_up": True, "rate_limited": True, "rate_limit_simulated": True}, "local",
     "rate_limit_simulated"),
    ({"est_tokens": 90_000}, {"local_up": True, "rate_limited": True}, "cloud", "long_context"),
    ({"reasoning": "high"}, {"local_up": True, "rate_limited": False}, "cloud", "reasoning_high"),
    ({"simple": True}, {"local_up": True, "rate_limited": False}, "local", "simple_task"),
    ({"simple": True, "est_tokens": 23_000}, {"local_up": True, "rate_limited": False}, "cloud", "agentic_prompt"),
    ({"privacy": "high", "est_tokens": 23_000}, {"local_up": True, "rate_limited": False}, "local", "privacy_high"),
    ({"simple": True}, {"local_up": False, "rate_limited": False}, "cloud", "default"),
])
def test_decision_table(feats_kw, state_kw, route, reason):
    from a3_router.policy import Features
    decision = decide(Features(**feats_kw), State(**state_kw), Settings())
    assert (decision.route, decision.reason) == (route, reason)


# ── transforms ─────────────────────────────────────────────────────────────────────────

def test_local_body_strips_cloud_only_state():
    history = [{"type": "reasoning", "encrypted_content": "opaque"},
               {"role": "assistant", "content": [{"type": "output_text", "text": "hi"}]}]
    body = to_local_body(request_body("hi", history=history), Config(local_model=LOCAL_MODEL))
    assert body["model"] == LOCAL_MODEL
    assert all(i.get("type") != "reasoning" for i in body["input"])
    assert "include" not in body and "prompt_cache_key" not in body
    assert body["reasoning"] == {"effort": "none"}


def test_cloud_headers_restore_account_and_identity():
    headers = cloud_headers({"authorization": AUTH["Authorization"], "host": "127.0.0.1:8787",
                             "user-agent": "codex_cli_rs/0.0.0 (Hermes Agent)", "originator": "codex_cli_rs"})
    assert headers["ChatGPT-Account-ID"] == "acct-test"
    assert headers["originator"] == "hermes-agent"
    assert "host" not in {k.lower() for k in headers}


# ── end to end through the app ─────────────────────────────────────────────────────────

def test_private_request_goes_local_and_never_touches_cloud(tmp_path):
    cloud, local = Upstream(), Upstream()
    client = make(tmp_path, cloud, local)
    resp = client.post("/responses", json=request_body("<!--route task=mail_extract privacy=high-->\n메일 분류"),
                       headers=AUTH)
    assert resp.status_code == 200 and b"response.completed" in resp.content
    assert cloud.calls == [] and local.posted()[0]["model"] == LOCAL_MODEL
    assert local.calls[0].url.path == "/v1/responses"
    entry = routing_log(tmp_path)[-1]
    assert (entry["route"], entry["reason"], entry["outcome"], entry["model"]) == \
        ("local", "privacy_high", "ok", LOCAL_MODEL)


def test_private_request_is_held_when_laptop_offline(tmp_path):
    cloud, local = Upstream(), Upstream(down=True)
    resp = make(tmp_path, cloud, local).post("/responses", json=request_body("내 연봉 메모 정리해줘"), headers=AUTH)
    assert resp.status_code == 200 and "held this request" in resp.text
    assert cloud.calls == []
    assert routing_log(tmp_path)[-1]["outcome"] == "held"


def test_reasoning_request_goes_cloud_with_oauth_passthrough(tmp_path):
    cloud, local = Upstream(), Upstream()
    make(tmp_path, cloud, local).post("/responses", json=request_body("두 JD를 비교 분석해줘"), headers=AUTH)
    assert local.calls == [] and len(cloud.calls) == 1
    sent = cloud.calls[0]
    assert sent.url.path.endswith("/responses") and sent.headers["authorization"] == AUTH["Authorization"]
    assert json.loads(sent.content)["model"] == "gpt-5.6-sol"
    log_text = (next((tmp_path / "logs" / "routing").glob("*.jsonl"))).read_text()
    assert fake_jwt() not in log_text


def test_simulated_429_flag_routes_local_and_is_reversible(tmp_path):
    cloud, local = Upstream(), Upstream()
    client = make(tmp_path, cloud, local)
    assert client.post("/router/ratelimit/on").json() == {"simulated": True}
    client.post("/responses", json=request_body("오늘 날씨 어때?"), headers=AUTH)
    assert routing_log(tmp_path)[-1]["reason"] == "rate_limit_simulated"
    assert client.post("/router/ratelimit/off").json() == {"simulated": False}
    client.post("/responses", json=request_body("면접 준비 전략 세워줘"), headers=AUTH)
    assert routing_log(tmp_path)[-1]["route"] == "cloud"


def test_real_cloud_429_falls_back_local_and_starts_cooldown(tmp_path):
    cloud, local = Upstream(status=429, body=b'{"error":{"message":"rate limited"}}'), Upstream()
    client = make(tmp_path, cloud, local)
    resp = client.post("/responses", json=request_body("면접 준비 전략 세워줘"), headers=AUTH)
    assert resp.status_code == 200
    entry = routing_log(tmp_path)[-1]
    assert (entry["route"], entry["fallback_from"], entry["first_failure"]) == ("local", "cloud", "http_429")
    client.post("/responses", json=request_body("면접 준비 전략 세워줘"), headers=AUTH)
    assert routing_log(tmp_path)[-1]["reason"] == "rate_limit" and len(cloud.calls) == 1


def test_error_inside_200_stream_is_logged_as_failure(tmp_path):
    cloud, local = Upstream(body=FAILED_SSE), Upstream()
    make(tmp_path, cloud, local).post("/responses", json=request_body("면접 준비 전략 세워줘"), headers=AUTH)
    assert routing_log(tmp_path)[-1]["outcome"] == "stream_error"


def test_missing_local_model_counts_as_local_down(tmp_path):
    cloud, local = Upstream(), Upstream(models=["llama3:8b"])
    make(tmp_path, cloud, local).post("/responses", json=request_body("내일 면접 몇 시야?"), headers=AUTH)
    entry = routing_log(tmp_path)[-1]
    assert (entry["route"], entry["local_detail"]) == ("cloud", "model_missing")


def test_non_streaming_json_response_is_logged_by_status(tmp_path):
    done = json.dumps({"status": "completed", "output": []}).encode()
    local = Upstream(body=done, content_type="application/json")
    body = {**request_body("<!--route task=mail_extract privacy=high-->\n메일"), "stream": False}
    resp = make(tmp_path, Upstream(), local).post("/v1/responses", json=body, headers=AUTH)
    assert resp.json()["status"] == "completed"
    assert routing_log(tmp_path)[-1]["outcome"] == "ok"


def test_local_body_keeps_only_function_tools():
    body = {**request_body("hi"), "tools": [{"type": "function", "name": "mcp__kairo__weather", "parameters": {}},
                                           {"type": "web_search"}]}
    local = to_local_body(body, Config(local_model=LOCAL_MODEL))
    assert [t["type"] for t in local["tools"]] == ["function"]
    only_builtin = to_local_body({**request_body("hi"), "tools": [{"type": "web_search"}], "tool_choice": "auto"},
                                 Config(local_model=LOCAL_MODEL))
    assert "tools" not in only_builtin and "tool_choice" not in only_builtin


# ── chat-only local path (course video requests) ───────────────────────────────────────

BIG_TOOLS = [{"type": "function", "name": f"mcp__kairo__tool_{i}", "description": "x" * 1200,
              "parameters": {"type": "object", "properties": {}}} for i in range(25)]


def agent_body(text: str, **kw) -> dict:
    """A turn as Hermes sends it from the web UI or Discord: the full tool catalog attached."""
    return {**request_body(text, **kw), "tools": BIG_TOOLS, "tool_choice": "auto", "parallel_tool_calls": True}


@pytest.mark.parametrize("text,privacy,reasoning,needs_tools", [
    ("What is 12 times 9?", "normal", "low", False),
    ("Explain step by step why the sky is blue.", "normal", "high", False),
    ("This is confidential: remind me my locker code is on a sticky note.", "high", "low", True),
    ('This is confidential: say "back online" in three words.', "high", "low", False),
    ("Hi, what can you do?", "normal", "low", False),
    ("Where do my applications stand?", "normal", "low", True),
    ("Draft a reply to Northwind", "normal", "low", True),
    ("I took a plane to the bankside", "normal", "low", False),   # whole words only: not plan / bank
])
def test_video_request_features(text, privacy, reasoning, needs_tools):
    feats = extract_features(agent_body(text))
    assert (feats.privacy, feats.reasoning, feats.needs_tools) == (privacy, reasoning, needs_tools)
    assert feats.est_tokens > Settings().local_agentic_max_tokens


@pytest.mark.parametrize("text", [
    "What's due this week?", "Anything due Friday?", "Do I have any conflicts on Thursday?",
    "Find me a free hour on Wednesday", "Any upcoming events?", "What's next on my agenda?",
    "Look up Harbor Analytics for me", "What should I focus on next?", "Have I heard back from anyone?",
    "What are my preferences?", "이번 주 마감 뭐 있어?",
])
def test_date_and_lookup_questions_get_tools(text):
    # These once went to Gemma without tools, which asked for a date or said it could not search.
    feats = extract_features(agent_body(text))
    assert (feats.privacy, feats.reasoning, feats.needs_tools) == ("normal", "low", True)
    decision = decide(feats, State(local_up=True, rate_limited=False), Settings())
    assert (decision.route, decision.reason) == ("cloud", "agentic_prompt")


def test_tool_history_keeps_tools():
    history = [{"type": "function_call", "name": "mcp__kairo__weather", "arguments": "{}", "call_id": "c1"},
               {"type": "function_call_output", "call_id": "c1", "output": "sunny"}]
    assert extract_features(agent_body("thanks!", history=history)).needs_tools is True


def test_simple_chat_goes_local_without_tool_catalog(tmp_path):
    cloud, local = Upstream(), Upstream()
    resp = make(tmp_path, cloud, local).post("/responses", json=agent_body("What is 12 times 9?"), headers=AUTH)
    assert resp.status_code == 200 and cloud.calls == []
    sent = local.posted()[0]
    assert "tools" not in sent and "tool_choice" not in sent and "parallel_tool_calls" not in sent
    assert "tools are not available" in sent["instructions"]
    entry = routing_log(tmp_path)[-1]
    assert (entry["route"], entry["reason"], entry["model"], entry["outcome"], entry["chat_only"]) == \
        ("local", "simple_task", LOCAL_MODEL, "ok", True)


def test_kairo_question_still_goes_cloud_with_tools(tmp_path):
    cloud, local = Upstream(), Upstream()
    make(tmp_path, cloud, local).post("/responses", json=agent_body("Where do my applications stand?"), headers=AUTH)
    assert local.calls == [] and cloud.posted()[0]["tools"]
    assert routing_log(tmp_path)[-1]["reason"] == "agentic_prompt"


def test_confidential_chat_goes_local_and_is_held_when_laptop_off(tmp_path):
    text = 'This is confidential: say "back online" in three words.'
    cloud, local = Upstream(), Upstream()
    make(tmp_path, cloud, local).post("/responses", json=agent_body(text), headers=AUTH)
    assert cloud.calls == [] and "tools" not in local.posted()[0]
    assert routing_log(tmp_path)[-1]["reason"] == "privacy_high"

    cloud, local = Upstream(), Upstream(down=True)
    resp = make(tmp_path / "off", cloud, local).post("/responses", json=agent_body(text), headers=AUTH)
    assert "held this request" in resp.text and cloud.calls == []
    entry = routing_log(tmp_path / "off")[-1]
    assert (entry["route"], entry["reason"], entry["outcome"], entry["model"]) == \
        ("held", "privacy_high_local_unavailable", "held", None)


def test_private_local_failure_mid_request_is_held_not_sent_to_cloud(tmp_path):
    cloud, local = Upstream(), Upstream(status=500, body=b'{"error":"boom"}', content_type="application/json")
    resp = make(tmp_path, cloud, local).post("/responses", json=agent_body("This is confidential: hi"), headers=AUTH)
    assert "held this request" in resp.text and cloud.calls == []
    entry = routing_log(tmp_path)[-1]
    assert (entry["route"], entry["outcome"], entry["first_failure"]) == ("held", "held", "http_500")


def test_plain_local_failure_retries_in_cloud(tmp_path):
    cloud, local = Upstream(), Upstream(status=500, body=b'{"error":"boom"}', content_type="application/json")
    resp = make(tmp_path, cloud, local).post("/responses", json=agent_body("What is 12 times 9?"), headers=AUTH)
    assert resp.status_code == 200 and len(cloud.calls) == 1 and len(local.calls) == 1
    assert cloud.posted()[0]["tools"]          # the cloud gets the original turn, tools included
    entry = routing_log(tmp_path)[-1]
    assert (entry["route"], entry["fallback_from"], entry["outcome"]) == ("cloud", "local", "ok")


def test_health_changes_are_logged_once(tmp_path, capsys):
    cloud, local = Upstream(), Upstream()
    client = make(tmp_path, cloud, local)
    client.get("/router/health")
    client.get("/router/health")
    local.down = True
    client.get("/router/health")
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines() if '"local_health"' in l]
    assert [(l["local_up"], l["detail"]) for l in lines] == [(True, "ok"), (False, "transport:ConnectError")]


TITLE_PROMPT = "You name chat sessions. Given the user's opening message, write a title ..."


def test_title_generation_is_an_auxiliary_local_task(tmp_path):
    body = request_body("Explain step by step why the sky is blue.", instructions=TITLE_PROMPT)
    feats = extract_features(body)
    assert (feats.task, feats.reasoning, feats.needs_tools) == ("title_generation", "low", False)
    assert decide(feats, State(local_up=True, rate_limited=False)).reason == "auxiliary_task"
    assert decide(feats, State(local_up=False, rate_limited=False)).route == "cloud"


def test_title_of_private_text_is_still_private():
    feats = extract_features(request_body("This is confidential: my locker code", instructions=TITLE_PROMPT))
    assert decide(feats, State(local_up=False, rate_limited=False)).route == "hold"
