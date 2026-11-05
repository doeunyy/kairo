"""Strict parsing of the local model's extraction output.

Anything outside the schema is rejected rather than ignored: an extra key such as
``"action": "reply"`` is a sign that email text steered the model, and the whole
extraction is discarded and logged.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime

from .status import MailType

FIELDS = {"type", "company", "role", "posting_id", "applied_on", "deadline", "confidence"}
REQUIRED = {"type", "company", "confidence"}
MAX_TEXT = 120
JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


class ExtractionError(ValueError):
    pass


@dataclass(frozen=True)
class Extraction:
    type: MailType
    company: str
    confidence: float
    role: str | None = None
    posting_id: str | None = None
    applied_on: date | None = None
    deadline: datetime | None = None


def _text(value: object, field: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ExtractionError(f"{field}: expected string")
    value = " ".join(value.split())
    if len(value) > MAX_TEXT:
        raise ExtractionError(f"{field}: longer than {MAX_TEXT} characters")
    return value


def parse_extraction(raw: str) -> Extraction:
    """Parse model output. Tolerates a code fence around the JSON object, nothing else."""
    match = JSON_OBJECT.search(raw.strip())
    if not match:
        raise ExtractionError("no JSON object")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"invalid JSON: {exc.msg}") from None
    if not isinstance(data, dict):
        raise ExtractionError("not an object")
    if extra := set(data) - FIELDS:
        raise ExtractionError(f"unexpected keys: {sorted(extra)}")
    if missing := REQUIRED - set(data):
        raise ExtractionError(f"missing keys: {sorted(missing)}")

    try:
        mail_type = MailType(data["type"])
    except ValueError:
        raise ExtractionError(f"type: unknown value {data['type']!r}") from None
    company = _text(data["company"], "company")
    if not company:
        raise ExtractionError("company: empty")
    confidence = data["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ExtractionError("confidence: expected number between 0 and 1")

    applied_on = deadline = None
    try:
        if data.get("applied_on"):
            applied_on = date.fromisoformat(data["applied_on"])
        if data.get("deadline"):
            deadline = datetime.fromisoformat(data["deadline"])
    except (TypeError, ValueError):
        raise ExtractionError("dates must be ISO 8601") from None

    return Extraction(type=mail_type, company=company, confidence=float(confidence),
                      role=_text(data.get("role"), "role"), posting_id=_text(data.get("posting_id"), "posting_id"),
                      applied_on=applied_on, deadline=deadline)
