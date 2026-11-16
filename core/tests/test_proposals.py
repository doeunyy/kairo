"""Proposal cards end to end: medium-tier mail waits for ✅, approval learns the sender's domain."""

from __future__ import annotations

import json
from email.message import EmailMessage
from email.utils import make_msgid

import pytest

from kairo_core import runner as runner_mod
from kairo_core import synthetic
from kairo_core.mail import parse_message
from kairo_core.proposals import ACCEPT, REJECT
from kairo_core.status import Status

from test_adapters import ME, STRANGER, make_kairo

GRADER = "Grader <grader@gmail.com>"


def mail(uid: int, sender: str, subject: str, body: str = "Hi Emily", auth_ok: bool = True):
    m = EmailMessage()
    domain = sender.rsplit("@", 1)[1].rstrip(">")
    m["Authentication-Results"] = synthetic.test_auth(domain, auth_ok)
    m["From"], m["Subject"], m["Message-ID"] = sender, subject, make_msgid(domain="t.test")
    m.set_content(body)
    return uid, parse_message(m.as_bytes())


def extraction(**fields) -> str:
    return json.dumps({"role": None, "posting_id": None, "applied_on": None, "deadline": None,
                       "confidence": 0.95, **fields})


@pytest.fixture
def kairo(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "is_quiet", lambda now: False)

    def build(mails, answers):
        k, discord = make_kairo(tmp_path, mails)
        k.extract = lambda m: answers[m.subject]
        return k, discord
    return build


def card(discord):
    return next(p for p in reversed(discord.posts) if p["embeds"])


def by_role(k):
    return {a.role: a for a in k.tracker.applications()}


def test_personal_sender_waits_for_approval_then_applies(kairo):
    subject = "Application received: Robotics Software Intern (CR-1042)"
    k, discord = kairo([mail(1, GRADER, subject)],
                       {subject: extraction(type="ack", company="Cardinal Robotics", role="Robotics Software Intern",
                                            posting_id="CR-1042")})
    assert k.sweep().outcomes == {"proposed": 1}
    assert by_role(k)["Robotics Software Intern"].status == Status.PREPARING      # not yet
    c = card(discord)
    assert "Preparing → Applied" in c["embeds"][0]["description"]
    assert "personal or school address" in json.dumps(c["embeds"][0])

    discord.reactions[(c["id"], ACCEPT)] = [STRANGER]
    assert [r.action for r in k.reviews()] == ["waiting"]                          # only the owner counts
    discord.reactions[(c["id"], ACCEPT)] = [ME]
    assert [r.action for r in k.reviews()] == ["applied"]
    row = by_role(k)["Robotics Software Intern"]
    assert row.status == Status.APPLIED and "gmail.com" not in row.email_domains   # never learned
    assert k.reviews() == []


def test_new_company_is_added_on_approval_and_its_domain_trusted_next_time(kairo):
    first, second = "Thanks for applying to Acme Robotics", "Acme Robotics interview invitation"
    answers = {first: extraction(type="ack", company="Acme Robotics", role="Controls Intern"),
               second: extraction(type="interview_request", company="Acme Robotics", role="Controls Intern")}
    k, discord = kairo([mail(1, "Acme Talent <talent@acme.test>", first)], answers)
    assert k.sweep().outcomes == {"proposed": 1}
    discord.reactions[(card(discord)["id"], ACCEPT)] = [ME]
    assert [r.action for r in k.reviews()] == ["applied"]
    acme = by_role(k)["Controls Intern"]
    assert (acme.company, acme.status, acme.email_domains) == ("Acme Robotics", Status.APPLIED, frozenset({"acme.test"}))

    k.mailbox_factory = lambda: __import__("test_adapters").FakeMailbox([mail(2, "talent@acme.test", second)])
    assert k.sweep().outcomes == {"applied": 1}                                     # high tier now
    assert by_role(k)["Controls Intern"].status == Status.INTERVIEW


def test_reject_leaves_the_tracker_alone(kairo):
    subject = "Application received: Robotics Software Intern (CR-1042)"
    k, discord = kairo([mail(1, GRADER, subject)], {subject: extraction(type="ack", company="Cardinal Robotics",
                                                                         posting_id="CR-1042")})
    k.sweep()
    discord.reactions[(card(discord)["id"], REJECT)] = [ME]
    discord.reactions[(card(discord)["id"], ACCEPT)] = [ME]                          # ❌ wins over ✅
    assert [r.action for r in k.reviews()] == ["rejected"]
    assert by_role(k)["Robotics Software Intern"].status == Status.PREPARING


def test_offer_from_a_trusted_domain_still_asks(kairo):
    subject = "Offer: Backend Engineer Intern (NW-221)"
    k, discord = kairo([mail(1, "Priya <talent@northwind.test>", subject)],
                       {subject: extraction(type="offer", company="Northwind Labs", posting_id="NW-221")})
    assert k.sweep().outcomes == {"proposed": 1}
    assert "always needs your OK" in json.dumps(card(discord)["embeds"][0])
    assert by_role(k)["Backend Engineer Intern"].status == Status.APPLIED


def test_edited_card_is_refused(kairo):
    subject = "Application received: Robotics Software Intern (CR-1042)"
    k, discord = kairo([mail(1, GRADER, subject)], {subject: extraction(type="ack", company="Cardinal Robotics",
                                                                         posting_id="CR-1042")})
    k.sweep()
    c = card(discord)
    c["embeds"][0]["footer"]["text"] = "✅ apply · sha256:" + "0" * 64
    discord.reactions[(c["id"], ACCEPT)] = [ME]
    assert [r.action for r in k.reviews()] == ["refused"]
    assert by_role(k)["Robotics Software Intern"].status == Status.PREPARING


def test_row_edited_meanwhile_is_a_conflict(kairo):
    subject = "Application received: Robotics Software Intern (CR-1042)"
    k, discord = kairo([mail(1, GRADER, subject)], {subject: extraction(type="ack", company="Cardinal Robotics",
                                                                         posting_id="CR-1042")})
    k.sweep()
    row = by_role(k)["Robotics Software Intern"]
    from kairo_core.pipeline import Update
    k.tracker.apply_update(Update(row.id, row.rev, {"next_action": "Edited by hand"}, row.edited_at), "manual")
    discord.reactions[(card(discord)["id"], ACCEPT)] = [ME]
    assert [r.action for r in k.reviews()] == ["conflict"]


def test_quiet_hours_hold_the_card_until_morning(kairo, monkeypatch):
    subject = "Application received: Robotics Software Intern (CR-1042)"
    k, discord = kairo([mail(1, GRADER, subject)], {subject: extraction(type="ack", company="Cardinal Robotics",
                                                                         posting_id="CR-1042")})
    monkeypatch.setattr(runner_mod, "is_quiet", lambda now: True)
    k.sweep()
    assert not any(p["embeds"] for p in discord.posts) and [r.action for r in k.reviews()] == []
    monkeypatch.setattr(runner_mod, "is_quiet", lambda now: False)
    assert [r.action for r in k.reviews()] == ["posted"]
    assert card(discord)


def test_lookalike_sender_is_flagged_and_never_extracted(kairo):
    subject = "Offer letter: Data Engineer Intern"
    k, discord = kairo([mail(1, "Northwind Labs HR <offers@northwind-labs.test>", subject)], {})
    assert k.sweep().outcomes == {"suspicious": 1}            # extract would KeyError if called
    alert = discord.posts[-1]["content"]
    assert "Suspicious" in alert and "Northwind Labs" in alert and not discord.posts[-1]["embeds"]
