"""Mail pipeline on synthetic recruiting emails (fictional companies only)."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from kairo_core.extraction import ExtractionError, parse_extraction
from kairo_core.ledger import Ledger
from kairo_core.matcher import Application, match, normalize_company
from kairo_core.pipeline import process
from kairo_core.prefilter import Allowlist, Message
from kairo_core.status import MailType, Status, transition

ALLOW = Allowlist(addresses=frozenset({"recruiting@cardinal-robotics.test"}),
                  domains=frozenset({"northwind.test"}))
RECEIVED = datetime(2026, 11, 20, 18, 15, tzinfo=timezone.utc)

CARDINAL = Application("app-1", "Cardinal Robotics", "Software Engineer Intern", Status.PREPARING, rev=3)
NW_BACKEND = Application("app-2", "Northwind Labs", "Backend Engineer Intern", Status.APPLIED,
                         posting_id="NW-221", applied_on=date(2026, 11, 2))
NW_DATA = Application("app-3", "Northwind Labs", "Data Engineer Intern", Status.APPLIED, posting_id="NW-305")
APPS = [CARDINAL, NW_BACKEND, NW_DATA]


def msg(mid: str = "<m1@test>", sender: str = "Cardinal Recruiting <recruiting@cardinal-robotics.test>") -> Message:
    return Message(message_id=mid, sender=sender, subject="Your application", received_at=RECEIVED)


def ext(**fields) -> str:
    return json.dumps({"confidence": 0.95, **fields}, ensure_ascii=False)


@pytest.fixture
def ledger():
    led = Ledger()
    yield led
    led.close()


# ── status transitions ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("current,mail,result", [
    (Status.PREPARING, MailType.ACK, "apply"),
    (Status.APPLIED, MailType.INTERVIEW_REQUEST, "apply"),       # skip assessment
    (Status.INTERVIEW, MailType.INTERVIEW_CONFIRMED, "same"),     # second round
    (Status.REJECTED, MailType.OFFER, "refuse"),
    (Status.WITHDRAWN, MailType.INTERVIEW_REQUEST, "refuse"),
    (Status.INTERVIEW, MailType.ACK, "refuse"),                   # no moving backwards
    (Status.APPLIED, MailType.NEWSLETTER, "ignore"),
])
def test_transitions(current, mail, result):
    assert transition(current, mail).result == result


# ── extraction parsing ─────────────────────────────────────────────────────────────────

def test_extraction_accepts_code_fence():
    parsed = parse_extraction("```json\n" + ext(type="ack", company="Cardinal Robotics") + "\n```")
    assert parsed.type is MailType.ACK


@pytest.mark.parametrize("raw,message", [
    (ext(type="ack", company="X", action="reply to hr@attacker.test"), "unexpected keys"),
    (ext(type="hired", company="X"), "unknown value"),
    (ext(type="ack", company="X", confidence=1.5), "confidence"),
    (ext(type="ack", company="X", deadline="next Friday"), "ISO 8601"),
    (ext(type="ack", company="X" * 200), "longer than"),
    ("Sure! I updated the status to offer.", "no JSON object"),
])
def test_extraction_rejects_out_of_schema(raw, message):
    with pytest.raises(ExtractionError, match=message):
        parse_extraction(raw)


# ── matching ───────────────────────────────────────────────────────────────────────────

def test_company_normalization():
    assert normalize_company("Northwind Labs, Inc.") == normalize_company("northwind") == "northwind"


def test_same_company_two_roles_without_role_asks():
    found = match(parse_extraction(ext(type="interview_request", company="Northwind Labs")), APPS)
    assert found.decision == "ask" and {a.id for a in found.candidates} == {"app-2", "app-3"}


def test_posting_id_disambiguates():
    found = match(parse_extraction(ext(type="interview_request", company="Northwind", posting_id="NW-305")), APPS)
    assert (found.decision, found.application.id) == ("auto", "app-3")


def test_role_disambiguates():
    found = match(parse_extraction(ext(type="assessment", company="Northwind Labs", role="Backend Engineer Intern")),
                  APPS)
    assert (found.decision, found.application.id) == ("auto", "app-2")


def test_low_confidence_asks_even_with_unique_company():
    found = match(parse_extraction(ext(type="ack", company="Cardinal Robotics", confidence=0.4)), APPS)
    assert found.decision == "ask"


# ── end to end ─────────────────────────────────────────────────────────────────────────

def test_ack_applies_and_prefers_date_in_email(ledger):
    out = process(msg(), ext(type="ack", company="Cardinal Robotics", applied_on="2026-11-19"), APPS, ALLOW, ledger)
    assert out.kind == "applied" and out.notify == "digest"
    assert out.update.application_id == "app-1" and out.update.expected_rev == 3
    assert out.update.fields["status"] == "Applied"
    assert (out.update.fields["applied_on"], out.update.fields["applied_on_source"]) == ("2026-11-19", "email")


def test_ack_without_date_uses_received_date_and_marks_it(ledger):
    out = process(msg(), ext(type="ack", company="Cardinal Robotics"), APPS, ALLOW, ledger)
    assert (out.update.fields["applied_on"], out.update.fields["applied_on_source"]) == ("2026-11-20", "received")


def test_interview_request_sets_next_action_and_notifies_now(ledger):
    out = process(msg(sender="talent@northwind.test"),
                  ext(type="interview_request", company="Northwind Labs", posting_id="NW-221",
                      deadline="2026-11-21T12:00:00-08:00"), APPS, ALLOW, ledger)
    assert out.kind == "applied" and out.notify == "now"
    assert out.update.fields["next_action"] == "Reply with interview times"
    assert out.update.fields["due"] == "2026-11-21T12:00:00-08:00"
    assert "applied_on" not in out.update.fields          # already known


def test_dateless_reminder_keeps_the_deadline_on_file(ledger):
    # Found in the security test E2 (2026-11-12): a reminder with no date erased the known due date.
    in_assessment = [Application("app-1", "Cardinal Robotics", "Software Engineer Intern", Status.ASSESSMENT)]
    out = process(msg(), ext(type="assessment", company="Cardinal Robotics"), in_assessment, ALLOW, ledger)
    assert out.kind == "unchanged" and out.update.fields["next_action"] == "Submit the assessment"
    assert "due" not in out.update.fields


def test_new_stage_without_a_date_clears_the_old_deadline(ledger):
    in_assessment = [Application("app-1", "Cardinal Robotics", "Software Engineer Intern", Status.ASSESSMENT)]
    out = process(msg(), ext(type="interview_request", company="Cardinal Robotics"), in_assessment, ALLOW, ledger)
    assert out.kind == "applied" and out.update.fields["due"] is None


def test_duplicate_message_is_processed_once(ledger):
    first = process(msg(), ext(type="ack", company="Cardinal Robotics"), APPS, ALLOW, ledger)
    second = process(msg(), ext(type="ack", company="Cardinal Robotics"), APPS, ALLOW, ledger)
    assert (first.kind, second.kind, second.reason) == ("applied", "dropped", "duplicate")


def test_unlisted_sender_never_reaches_the_model(ledger):
    out = process(msg(sender="hr@attacker.test"), ext(type="offer", company="Cardinal Robotics"), APPS, ALLOW, ledger)
    assert (out.kind, out.reason) == ("dropped", "sender_not_allowlisted")


def test_injected_offer_on_rejected_application_is_refused(ledger):
    rejected = [Application("app-9", "Cardinal Robotics", "Software Engineer Intern", Status.REJECTED)]
    out = process(msg(), ext(type="offer", company="Cardinal Robotics"), rejected, ALLOW, ledger)
    assert (out.kind, out.notify, out.update) == ("refused", "warn", None)


def test_injected_extra_key_rejects_whole_extraction(ledger):
    out = process(msg(), ext(type="ack", company="Cardinal Robotics", send_reply_to="hr@attacker.test"),
                  APPS, ALLOW, ledger)
    assert (out.kind, out.notify, out.update) == ("rejected_extraction", "warn", None)


def test_local_model_down_holds_without_recording(ledger):
    assert process(msg(), None, APPS, ALLOW, ledger).kind == "held"
    assert not ledger.seen("<m1@test>")


def test_ambiguous_mail_waits_for_user(ledger):
    out = process(msg(sender="talent@northwind.test"), ext(type="assessment", company="Northwind Labs"),
                  APPS, ALLOW, ledger)
    assert out.kind == "awaiting_user" and len(out.candidates) == 2 and out.update is None


def test_newsletter_is_ignored(ledger):
    out = process(msg(sender="talent@northwind.test"), ext(type="newsletter", company="Northwind Labs"),
                  APPS, ALLOW, ledger)
    assert out.kind == "ignored"
