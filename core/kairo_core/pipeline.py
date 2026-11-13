"""One recruiting email in, one decision out.

``process`` never writes to the tracker. It returns an ``Outcome`` whose ``update`` is a
plan; the Notion adapter applies it only if the application's ``rev`` is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .extraction import Extraction, ExtractionError, parse_extraction
from .ledger import Ledger
from .matcher import Application, match
from .prefilter import Allowlist, Message, prefilter
from .status import MailType, transition

NEXT_ACTION: dict[MailType, str] = {
    MailType.ASSESSMENT: "Submit the assessment",
    MailType.INTERVIEW_REQUEST: "Reply with interview times",
    MailType.INTERVIEW_CONFIRMED: "Prepare for the interview",
    MailType.OFFER: "Review the offer",
}
# Mail that needs the user before the next scheduled briefing.
NOTIFY_NOW = {MailType.ASSESSMENT, MailType.INTERVIEW_REQUEST, MailType.INTERVIEW_CONFIRMED, MailType.OFFER}


@dataclass(frozen=True)
class Update:
    application_id: str
    expected_rev: int
    fields: dict[str, Any]
    expected_edited_at: str | None = None


@dataclass(frozen=True)
class Outcome:
    kind: str          # applied | unchanged | awaiting_user | unknown_application | refused |
                       # ignored | rejected_extraction | dropped | held
    reason: str
    extraction: Extraction | None = None
    update: Update | None = None
    candidates: tuple[Application, ...] = field(default_factory=tuple)
    notify: str = "none"   # now | digest | warn | none


def plan_update(extraction: Extraction, app: Application, message: Message) -> Outcome:
    """Status change and dates for a known application (also used after the user picks one)."""
    step = transition(app.status, extraction.type)
    if step.result == "ignore":
        return Outcome("ignored", step.reason, extraction)
    if step.result == "refuse":
        return Outcome("refused", step.reason, extraction, notify="warn")

    received: date = message.received_at.date()
    fields: dict[str, Any] = {"last_contact": received.isoformat(), "evidence_message_id": message.message_id}
    if step.result == "apply":
        fields["status"] = step.target.value
    if extraction.type == MailType.ACK and app.applied_on is None:
        # Prefer the date written in the email; the receipt date is only a stand-in.
        fields["applied_on"] = (extraction.applied_on or received).isoformat()
        fields["applied_on_source"] = "email" if extraction.applied_on else "received"
    if extraction.type in NEXT_ACTION:
        fields["next_action"] = NEXT_ACTION[extraction.type]
        if extraction.deadline:
            fields["due"] = extraction.deadline.isoformat()
        elif step.result == "apply":
            # A new stage: the previous stage's deadline no longer applies. A dateless reminder
            # within the same stage keeps the deadline already on file.
            fields["due"] = None
    elif extraction.type == MailType.REJECTION:
        fields["next_action"] = None
        fields["due"] = None

    kind = "applied" if step.result == "apply" else "unchanged"
    notify = "now" if extraction.type in NOTIFY_NOW else "digest"
    return Outcome(kind, step.reason, extraction, Update(app.id, app.rev, fields, app.edited_at), notify=notify)


def process(message: Message, raw_extraction: str | None, applications: list[Application],
            allowlist: Allowlist, ledger: Ledger) -> Outcome:
    verdict = prefilter(message, allowlist, ledger)
    if not verdict.passed:
        if verdict.reason != "duplicate":
            ledger.record(message.message_id, f"dropped:{verdict.reason}")
        return Outcome("dropped", verdict.reason)

    if raw_extraction is None:
        # Local model unavailable: leave unrecorded so the next sweep retries it.
        return Outcome("held", "local_model_unavailable")

    try:
        extraction = parse_extraction(raw_extraction)
    except ExtractionError as exc:
        ledger.record(message.message_id, "rejected_extraction")
        return Outcome("rejected_extraction", str(exc), notify="warn")

    if extraction.type in (MailType.NEWSLETTER, MailType.OTHER):
        ledger.record(message.message_id, "ignored")
        return Outcome("ignored", f"type_{extraction.type.value}", extraction)

    found = match(extraction, applications)
    if found.decision == "none":
        ledger.record(message.message_id, "unknown_application")
        return Outcome("unknown_application", found.reason, extraction, notify="now")
    if found.decision == "ask":
        ledger.record(message.message_id, "awaiting_user")
        return Outcome("awaiting_user", found.reason, extraction, candidates=found.candidates, notify="now")

    outcome = plan_update(extraction, found.application, message)
    ledger.record(message.message_id, outcome.kind, found.application.id)
    return outcome
