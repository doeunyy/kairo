"""Sender authentication, the recruiting pre-sort and trust tiers (fictional companies only)."""

from __future__ import annotations

from datetime import datetime, timezone
from email.message import EmailMessage

import pytest

from kairo_core.ledger import Ledger
from kairo_core.mail import OUTBOX_HEADER, parse_message
from kairo_core.mailauth import AuthResult, authenticated_domain, parse_auth_results
from kairo_core.prefilter import Allowlist, Message, prefilter
from kairo_core.triage import KnownCompany, is_recruiting, learnable, trust

OWN = "emily.kim.test@gmail.com"
NOW = datetime(2026, 11, 20, 18, 15, tzinfo=timezone.utc)
NORTHWIND = KnownCompany("Northwind Labs", frozenset({"northwind.test"}))
CARDINAL = KnownCompany("Cardinal Robotics", frozenset({"cardinal-robotics.test"}))
COMPANIES = [NORTHWIND, CARDINAL]


def gmail_verdict(domain: str, dmarc: str = "pass", dkim: str = "pass", spf: str = "pass") -> str:
    return (f"mx.google.com; dkim={dkim} header.i=@{domain} header.s=s1 header.b=AbC; "
            f"spf={spf} (google.com: domain of hr@{domain} designates 1.2.3.4 as permitted sender) "
            f"smtp.mailfrom=hr@{domain}; dmarc={dmarc} (p=REJECT sp=REJECT dis=NONE) header.from={domain}")


def raw(sender: str, subject: str = "Interview invitation", body: str = "Hi Emily", auth: list[str] = (),
        **headers) -> Message:
    m = EmailMessage()
    for value in auth:                       # listed top first, as Gmail stores them
        m["Authentication-Results"] = value
    m["From"], m["Subject"], m["Message-ID"] = sender, subject, "<t1@x.test>"
    for key, value in headers.items():
        m[key.replace("_", "-")] = value
    m.set_content(body)
    return parse_message(m.as_bytes(), NOW)


# ── Authentication-Results ─────────────────────────────────────────────────────────────

def test_gmail_verdict_is_read():
    a = parse_auth_results([gmail_verdict("northwind.test")])
    assert (a.dkim, a.dkim_domain, a.spf, a.spf_domain, a.dmarc, a.dmarc_from) == \
        ("pass", "northwind.test", "pass", "northwind.test", "pass", "northwind.test")
    assert authenticated_domain(a, "northwind.test")


def test_only_the_topmost_header_counts():
    forged = gmail_verdict("northwind.test")
    # A sender can add its own "pass" header, but Gmail's real one is always on top.
    msg = raw("HR <hr@northwind.test>", auth=[gmail_verdict("northwind.test", dmarc="fail", dkim="fail"), forged])
    assert msg.auth.dmarc == "fail" and not authenticated_domain(msg.auth, "northwind.test")


def test_header_from_another_server_is_ignored():
    assert parse_auth_results(["evil.test; dkim=pass header.d=northwind.test; dmarc=pass header.from=northwind.test"]) is None
    assert parse_auth_results([]) is None


def test_dkim_must_align_with_from():
    a = AuthResult("mx.google.com", dkim="pass", dkim_domain="mailer.test", dmarc="none")
    assert not authenticated_domain(a, "northwind.test")
    assert authenticated_domain(AuthResult("mx.google.com", dkim="pass", dkim_domain="mail.northwind.test"),
                                "northwind.test")


# ── recruiting pre-sort ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("sender,subject,body,expected", [
    ("no-reply@greenhouse.io", "Thanks!", "", True),                           # ATS domain alone
    ("a@b.test", "Your application to Northwind Labs", "", True),
    ("a@b.test", "Lunch?", "Are you free for an interview prep chat?", True),  # keyword in body
    ("a@b.test", "면접 일정 안내", "", True),
    ("a@b.test", "Weekly digest", "Top stories this week", False),
    ("mom@b.test", "Dinner Sunday", "Bring dessert", False),
])
def test_is_recruiting(sender, subject, body, expected):
    assert is_recruiting(Message("<x>", sender, subject, NOW, body)) is expected


# ── trust tiers ────────────────────────────────────────────────────────────────────────

def tier(msg: Message) -> tuple[str, str]:
    t = trust(msg, COMPANIES, OWN)
    return t.tier, t.reason


def test_known_company_domain_is_high():
    assert tier(raw("Northwind Recruiting <hr@northwind.test>", auth=[gmail_verdict("northwind.test")])) == \
        ("high", "known_domain:Northwind Labs")
    sub = raw("hr@mail.northwind.test", auth=[gmail_verdict("mail.northwind.test")])
    assert tier(sub)[0] == "high"


def test_ats_writing_about_a_tracked_company_is_high():
    msg = raw("Northwind Labs <no-reply@greenhouse.io>", subject="Northwind Labs: next steps",
              auth=[gmail_verdict("greenhouse.io")])
    assert tier(msg) == ("high", "ats:Northwind Labs")
    other = raw("Acme <no-reply@greenhouse.io>", subject="Acme: next steps", auth=[gmail_verdict("greenhouse.io")])
    assert tier(other) == ("medium", "ats_unknown_company")


def test_new_and_personal_senders_are_medium():
    assert tier(raw("hr@acme.test", auth=[gmail_verdict("acme.test")])) == ("medium", "new_domain")
    assert tier(raw("Grader <grader@gmail.com>", auth=[gmail_verdict("gmail.com")])) == ("medium", "personal_domain")
    assert tier(raw("ta@usc.edu", auth=[gmail_verdict("usc.edu")])) == ("medium", "personal_domain")


def test_own_address_without_a_gmail_verdict_is_medium():
    # Mail the account sends to itself has no outside delivery; only the owner can create it.
    assert tier(raw(f"Emily Kim <{OWN}>")) == ("medium", "own_address_unverified")


def test_unauthenticated_and_failed_mail_is_low():
    assert tier(raw("hr@northwind.test")) == ("low", "no_gmail_verdict")     # e.g. no Gmail header at all
    failed = raw("hr@northwind.test", auth=[gmail_verdict("northwind.test", dmarc="fail", dkim="fail", spf="fail")])
    assert tier(failed)[0] == "low" and tier(failed)[1].startswith("auth_failed")


@pytest.mark.parametrize("sender,domain", [
    ("Recruiting <offers@northwind-labs.test>", "northwind-labs.test"),       # lookalike domain
    ("Northwind Labs HR <northwind.hr@gmail.com>", "gmail.com"),              # borrowed display name
])
def test_lookalikes_of_a_tracked_company_are_low(sender, domain):
    assert tier(raw(sender, auth=[gmail_verdict(domain)])) == ("low", "lookalike:Northwind Labs")


def test_reply_to_elsewhere_downgrades_a_known_domain():
    msg = raw("hr@northwind.test", auth=[gmail_verdict("northwind.test")], Reply_To="collect@payout.test")
    assert tier(msg) == ("medium", "reply_to_elsewhere:Northwind Labs")


def test_learnable_domains():
    assert learnable("acme.test", OWN)
    assert not any(learnable(d, OWN) for d in ("gmail.com", "usc.edu", "greenhouse.io", "hire.lever.co", ""))


# ── Kairo's own sent mail ──────────────────────────────────────────────────────────────

def test_kairo_sent_copy_is_skipped_only_from_our_own_address():
    led = Ledger()
    try:
        ours = raw(f"Emily Kim <{OWN}>", **{OUTBOX_HEADER: "d-123"})
        assert ours.kairo_sent
        assert prefilter(ours, Allowlist(frozenset({OWN})), led, OWN).reason == "sent_by_kairo"
        # Anyone else can add the header; it never hides their mail.
        theirs = raw("hr@northwind.test", **{OUTBOX_HEADER: "d-123"})
        assert prefilter(theirs, Allowlist(domains=frozenset({"northwind.test"})), led, OWN).passed
    finally:
        led.close()
