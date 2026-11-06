"""Routing policy: pure functions from a Responses request to a routing decision.

Kept free of I/O so the decision table can be unit-tested and quoted in the
performance report exactly as it runs.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

# Skills and cron prompts tag their task on the first line, e.g.
#   <!--route task=mail_extract privacy=high-->
ROUTE_TAG = re.compile(r"<!--\s*route\s+([^>]*?)-->", re.IGNORECASE)
TAG_PAIR = re.compile(r"(\w+)=([\w.-]+)")

# Free-form messages without a tag fall back to these word lists.
PRIVACY_TERMS = (
    "연봉", "희망연봉", "희망 연봉", "급여", "처우", "개인 메모", "개인메모", "비밀",
    "salary", "compensation", "private note", "ssn", "passport",
)
REASONING_TERMS = (
    "비교", "분석", "전략", "리서치", "조사", "면접 준비", "jd",
    "compare", "analyze", "analyse", "research", "strategy", "plan",
)
SIMPLE_MAX_CHARS = 280


@dataclass(frozen=True)
class Settings:
    # Requests estimated above this go to the cloud: the local model's served
    # context (65,536) minus room for output and estimation error.
    local_max_tokens: int = 40_000
    # Above this, a "simple" turn is really an agent turn carrying the full tool catalog.
    # Measured 2026-11-04: Gemma 4 e4b with Kairo's ~23K-token prompt (MCP + built-in tools)
    # returned empty replies and ignored tool arguments; gpt-5.6-sol answered correctly.
    # In the cloud the API-server turn is ~11K tokens and Gemma again skipped the tools, so the
    # line sits below any prompt that carries the tool catalog (titles ~0.3K stay local).
    local_agentic_max_tokens: int = 8_000


@dataclass
class Features:
    task: str = "chat"
    privacy: str = "normal"      # normal | high
    reasoning: str = "low"       # low | high
    simple: bool = False
    est_tokens: int = 0
    tagged: bool = False


@dataclass
class State:
    local_up: bool
    rate_limited: bool
    rate_limit_simulated: bool = False


@dataclass
class Decision:
    route: str                   # local | cloud | hold
    reason: str
    features: Features
    state: State
    fallback_allowed: bool = field(default=False)

    def log_fields(self) -> dict[str, Any]:
        return {
            "decision": self.route,
            "reason": self.reason,
            "features": asdict(self.features),
            "state": asdict(self.state),
        }


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


def last_user_text(body: dict[str, Any]) -> str:
    items = body.get("input")
    if isinstance(items, str):
        return items
    if not isinstance(items, list):
        return ""
    for item in reversed(items):
        if isinstance(item, dict) and item.get("role") == "user":
            return _text_of(item.get("content"))
    return ""


def estimate_tokens(body: dict[str, Any]) -> int:
    # About 3 characters per token across mixed Korean/English prompts plus JSON tool
    # schemas. Deliberately rough: it only has to tell 15K from 60K.
    size = len(body.get("instructions") or "")
    size += len(json.dumps(body.get("input", ""), ensure_ascii=False))
    size += len(json.dumps(body.get("tools", []), ensure_ascii=False))
    return size // 3


def extract_features(body: dict[str, Any]) -> Features:
    text = last_user_text(body)
    feats = Features(est_tokens=estimate_tokens(body))
    match = ROUTE_TAG.search(text) or ROUTE_TAG.search(body.get("instructions") or "")
    if match:
        tags = dict(TAG_PAIR.findall(match.group(1)))
        feats.tagged = True
        feats.task = tags.get("task", feats.task)
        feats.privacy = "high" if tags.get("privacy") == "high" else "normal"
        feats.reasoning = "high" if tags.get("reasoning") == "high" else "low"
        feats.simple = tags.get("simple") == "true" or (feats.reasoning == "low" and feats.privacy == "normal"
                                                        and tags.get("simple") != "false")
        return feats
    lowered = text.lower()
    if any(term in lowered for term in PRIVACY_TERMS):
        feats.privacy = "high"
    if any(term in lowered for term in REASONING_TERMS):
        feats.reasoning = "high"
    feats.simple = feats.reasoning == "low" and len(text) <= SIMPLE_MAX_CHARS
    return feats


def decide(feats: Features, state: State, settings: Settings = Settings()) -> Decision:
    """Ordered rules; the first that matches wins (see docs: routing policy)."""
    too_long_for_local = feats.est_tokens > settings.local_max_tokens

    if feats.privacy == "high":
        if state.local_up and not too_long_for_local:
            return Decision("local", "privacy_high", feats, state)
        # Fail closed: private work never silently moves to the cloud.
        return Decision("hold", "privacy_high_local_unavailable" if not state.local_up else "privacy_high_too_long",
                        feats, state)

    if state.rate_limited and state.local_up and not too_long_for_local:
        reason = "rate_limit_simulated" if state.rate_limit_simulated else "rate_limit"
        return Decision("local", reason, feats, state)

    if too_long_for_local:
        return Decision("cloud", "long_context", feats, state)

    if feats.reasoning == "high":
        return Decision("cloud", "reasoning_high", feats, state, fallback_allowed=state.local_up)

    if feats.simple and state.local_up and feats.est_tokens > settings.local_agentic_max_tokens:
        return Decision("cloud", "agentic_prompt", feats, state, fallback_allowed=True)

    if feats.simple and state.local_up:
        return Decision("local", "simple_task", feats, state, fallback_allowed=False)

    return Decision("cloud", "default", feats, state, fallback_allowed=state.local_up)
