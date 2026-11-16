"""Which mail is about a job search, and how far to trust who sent it. Code only, no model.

``is_recruiting`` is a cheap pre-sort: mail from an applicant-tracking system (ATS) or with
recruiting words goes on to the extractor; everything else is only summarized.

``trust`` decides what Kairo may do with a recruiting email:

- ``high``: Gmail authenticated the sender's domain (DMARC, or aligned DKIM), and that domain is
  one the tracker already holds for the company, or it is an ATS writing about a tracked company.
  Status changes may apply automatically (Offer and Rejected still ask; see pipeline).
- ``medium``: authenticated, but a domain Kairo has not seen for any company, or a personal or
  school address. Changes wait for the user's ✅ in Discord.
- ``low``: Gmail did not vouch for the sender, authentication failed, or the sender borrows the
  name of a tracked company from a domain that company does not use. Kairo only warns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .mailauth import aligned, authenticated_domain
from .matcher import normalize_company
from .prefilter import Message

ATS_DOMAINS = frozenset({
    "greenhouse.io", "greenhouse-mail.io", "lever.co", "hire.lever.co", "myworkday.com", "myworkdayjobs.com",
    "ashbyhq.com", "smartrecruiters.com", "icims.com", "jobvite.com", "workablemail.com", "workable.com",
    "successfactors.com", "taleo.net", "bamboohr.com", "recruitee.com", "teamtailor.com",
})
# Never learned as a company's own domain: anyone can have an address here.
PERSONAL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com", "msn.com",
    "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com", "zoho.com", "gmx.com",
    "naver.com", "daum.net", "hanmail.net", "kakao.com",
})
SCHOOL_SUFFIXES = (".edu", ".ac.kr", ".ac.uk", ".edu.au")
RECRUITING_WORDS = re.compile(
    r"\b(applications?|applied|apply|applying|interview(?:s|ing)?|phone screen|onsite|assessment|coding (?:challenge|test)|"
    r"take[- ]home|offer letter|job offer|offer|recruit(?:er|ers|ing|ment)|hiring|candidate|candidacy|"
    r"requisition|internship|position)\b|면접|지원|채용|서류|합격|불합격|코딩\s*테스트",
    re.IGNORECASE)
SCAN_CHARS = 2_000


@dataclass(frozen=True)
class KnownCompany:
    name: str
    domains: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Trust:
    tier: str                 # high | medium | low
    reason: str
    company: str | None = None


def _matches(domain: str, domains) -> bool:
    """The domain is one of these, or a subdomain of one (mail.northwind.test → northwind.test)."""
    return any(domain == d or domain.endswith("." + d) for d in domains)


def is_ats(domain: str) -> bool:
    return _matches(domain, ATS_DOMAINS)


def is_personal(domain: str) -> bool:
    return domain in PERSONAL_DOMAINS or domain.endswith(SCHOOL_SUFFIXES)


def learnable(domain: str, own_address: str = "") -> bool:
    """May this domain be recorded as a company's own after the user approves a change?"""
    own_domain = own_address.rsplit("@", 1)[-1].lower() if "@" in own_address else ""
    return bool(domain) and not is_personal(domain) and not is_ats(domain) and domain != own_domain


def is_recruiting(message: Message) -> bool:
    if is_ats(message.domain):
        return True
    return bool(RECRUITING_WORDS.search(f"{message.subject}\n{message.body[:SCAN_CHARS]}"))


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9가-힣]", "", text.lower())


def _named(company: KnownCompany, message: Message, *, in_subject: bool) -> bool:
    """Does the sender's name, domain (or, for an ATS, the subject) use this company's name?"""
    name = _compact(normalize_company(company.name))
    if len(name) < 3:
        return False
    places = [message.display_name, message.domain.split(".")[0] if not is_ats(message.domain) else ""]
    if in_subject:
        places.append(message.subject)
    return any(name in _compact(p) for p in places if p)


def trust(message: Message, companies: list[KnownCompany], own_address: str = "") -> Trust:
    domain = message.domain
    if not domain:
        return Trust("low", "no_sender_address")
    own = bool(own_address) and message.address == own_address.lower()
    if message.auth is None:
        if own:
            # No Gmail verdict at all: not received from outside (only the account owner can place
            # such a message, e.g. a note to self). Treated like any new sender.
            return Trust("medium", "own_address_unverified")
        return Trust("low", "no_gmail_verdict")
    if not authenticated_domain(message.auth, domain):
        return Trust("low", f"auth_failed:dmarc={message.auth.dmarc},dkim={message.auth.dkim}")

    for c in companies:
        if c.domains and not is_ats(domain) and not _matches(domain, c.domains) and _named(c, message, in_subject=False):
            return Trust("low", f"lookalike:{c.name}", c.name)

    for c in companies:
        if c.domains and _matches(domain, c.domains):
            reply = Message("", message.reply_to, "", message.received_at).domain if message.reply_to else ""
            if reply and not _matches(reply, c.domains) and not aligned(reply, domain):
                return Trust("medium", f"reply_to_elsewhere:{c.name}", c.name)
            return Trust("high", f"known_domain:{c.name}", c.name)

    if is_ats(domain):
        named = [c for c in companies if _named(c, message, in_subject=True)]
        if len(named) == 1:
            return Trust("high", f"ats:{named[0].name}", named[0].name)
        return Trust("medium", "ats_unknown_company")

    if own:
        return Trust("medium", "own_address")
    if is_personal(domain):
        return Trust("medium", "personal_domain")
    return Trust("medium", "new_domain")
