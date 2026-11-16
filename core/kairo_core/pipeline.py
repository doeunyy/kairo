"""One email in, one decision out.

``process`` never writes to the tracker. It returns an ``Outcome`` whose ``update`` (or
``new_application``) is a plan. The runner applies a plan only when the sender's trust tier
allows it (``applied``/``unchanged``); a ``proposed`` plan waits for the user's ✅ in Discord.
Either way the Notion adapter writes only if the application's ``rev`` is unchanged.

Tiers (see ``triage``): high may change a status by itself, except to Offer or Rejected;
medium always asks; low never reaches the model.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any

from .extraction import Extraction, ExtractionError, parse_extraction
from .ledger import Ledger
from .matcher import Application, match, normalize_company
from .prefilter import Message, prefilter
from .status import TARGET, MailType, Status, transition
from .triage import Trust

NEXT_ACTION: dict[MailType, str] = {
    MailType.ASSESSMENT: "Submit the assessment",
    MailType.INTERVIEW_REQUEST: "Reply with interview times",
    MailType.INTERVIEW_CONFIRMED: "Prepare for the interview",
    MailType.OFFER: "Review the offer",
}
# Mail that needs the user before the next scheduled briefing.
NOTIFY_NOW = {MailType.ASSESSMENT, MailType.INTERVIEW_REQUEST, MailType.INTERVIEW_CONFIRMED, MailType.OFFER}
# Outcomes too consequential to take from email alone, whoever sent it.
ALWAYS_ASK = {Status.OFFER, Status.REJECTED}


@dataclass(frozen=True)
class Update:
    application_id: str
    expected_rev: int
    fields: dict[str, Any]
    expected_edited_at: str | None = None


@dataclass(frozen=True)
class Outcome:
    kind: str          # applied | unchanged | proposed | awaiting_user | refused | ignored |
                       # rejected_extraction | suspicious | inbox_only | dropped | held
    reason: str
    extraction: Extraction | None = None
    update: Update | None = None
    candidates: tuple[Application, ...] = field(default_factory=tuple)
    notify: str = "none"   # now | digest | warn | none
    trust: Trust | None = None
    new_application: dict[str, Any] | None = None    # proposed row for a company Kairo isn't tracking
    application: Application | None = None           # the row an update is for


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


def new_application(extraction: Extraction, message: Message) -> dict[str, Any]:
    """A row for mail about an application the tracker doesn't have yet (always proposed)."""
    target = TARGET.get(extraction.type)
    status = target if target and target not in ALWAYS_ASK else Status.APPLIED
    received = message.received_at.date()
    row: dict[str, Any] = {"company": extraction.company, "role": extraction.role or "(role not stated)",
                           "status": status.value, "posting_id": extraction.posting_id,
                           "applied_on": (extraction.applied_on or received).isoformat(),
                           "applied_on_source": "email" if extraction.applied_on else "received",
                           "last_contact": received.isoformat(), "evidence_message_id": message.message_id}
    if extraction.type in NEXT_ACTION:
        row["next_action"] = NEXT_ACTION[extraction.type]
    if extraction.deadline:
        row["due"] = extraction.deadline.isoformat()
    return {k: v for k, v in row.items() if v is not None}


def process(message: Message, raw_extraction: str | None, applications: list[Application],
            trust: Trust, ledger: Ledger, own_address: str = "", *, recruiting: bool = True) -> Outcome:
    verdict = prefilter(message, ledger, own_address)
    if not verdict.passed:
        if verdict.reason != "duplicate":
            ledger.record(message.message_id, f"dropped:{verdict.reason}")
        return Outcome("dropped", verdict.reason)

    if not recruiting:
        ledger.record(message.message_id, "inbox_only")
        return Outcome("inbox_only", "not_recruiting", trust=trust)
    if trust.tier == "low":
        # Never shown to the model: a spoofed or unverified sender only earns a warning.
        ledger.record(message.message_id, "suspicious")
        return Outcome("suspicious", trust.reason, notify="warn", trust=trust)

    if raw_extraction is None:
        # Local model unavailable: leave unrecorded so the next sweep retries it.
        return Outcome("held", "local_model_unavailable", trust=trust)

    try:
        extraction = parse_extraction(raw_extraction)
    except ExtractionError as exc:
        ledger.record(message.message_id, "rejected_extraction")
        return Outcome("rejected_extraction", str(exc), notify="warn", trust=trust)

    if extraction.type in (MailType.NEWSLETTER, MailType.OTHER):
        ledger.record(message.message_id, "ignored")
        return Outcome("ignored", f"type_{extraction.type.value}", extraction, trust=trust)

    if trust.tier == "high" and trust.company and \
            normalize_company(trust.company) != normalize_company(extraction.company):
        # A trusted sender writing about some other company: that company never vouched for it.
        trust = Trust("medium", f"company_mismatch:{trust.company}")

    found = match(extraction, applications)
    if found.decision == "none":
        ledger.record(message.message_id, "proposed")
        return Outcome("proposed", "new_application", extraction, notify="now", trust=trust,
                       new_application=new_application(extraction, message))
    if found.decision == "ask":
        ledger.record(message.message_id, "awaiting_user")
        return Outcome("awaiting_user", found.reason, extraction, candidates=found.candidates, notify="now",
                       trust=trust)

    outcome = plan_update(extraction, found.application, message)
    outcome = replace(outcome, trust=trust, application=found.application)
    if outcome.update is not None:
        target = outcome.update.fields.get("status")
        if trust.tier != "high":
            outcome = replace(outcome, kind="proposed", reason=f"sender_{trust.reason}", notify="now")
        elif target in {s.value for s in ALWAYS_ASK}:
            outcome = replace(outcome, kind="proposed", reason=f"always_ask_{target.lower()}", notify="now")
    ledger.record(message.message_id, outcome.kind, found.application.id)
    return outcome
