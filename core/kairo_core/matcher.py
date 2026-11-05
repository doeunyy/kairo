"""Link an extraction to one tracked application, or say that the user must choose."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from .extraction import Extraction
from .status import Status

COMPANY_SUFFIXES = re.compile(r"\b(inc|llc|ltd|corp|corporation|co|company|labs?)\b|주식회사|\(주\)")
NON_WORD = re.compile(r"[^\w\s]")
# Generic title words are left out so "Backend Engineer Intern" and "Data Engineer Intern"
# are compared on what tells them apart.
ROLE_STOPWORDS = {
    "the", "a", "an", "of", "for", "and", "position", "role", "포지션", "직무",
    "intern", "internship", "engineer", "engineering", "developer", "software", "junior", "senior",
    "new", "grad", "인턴", "엔지니어", "개발자", "신입",
}

COMPANY_SCORE, ROLE_STRONG, ROLE_WEAK, POSTING_ID_SCORE = 0.6, 0.3, 0.15, 1.0
AUTO_MIN_SCORE, AUTO_MIN_GAP, AUTO_MIN_CONFIDENCE = 0.6, 0.3, 0.7


@dataclass(frozen=True)
class Application:
    id: str
    company: str
    role: str
    status: Status
    posting_id: str | None = None
    applied_on: date | None = None
    rev: int = 0            # bumped on every write; checked before the next one
    edited_at: str | None = None   # tracker's last-edited time, so a user's manual edit is noticed too


@dataclass(frozen=True)
class Match:
    decision: str                        # auto | ask | none
    application: Application | None
    candidates: tuple[Application, ...]
    reason: str


def normalize_company(name: str) -> str:
    name = COMPANY_SUFFIXES.sub(" ", name.lower())
    return " ".join(NON_WORD.sub(" ", name).split())


def role_tokens(role: str) -> set[str]:
    return {t for t in NON_WORD.sub(" ", role.lower()).split() if t not in ROLE_STOPWORDS}


def score(extraction: Extraction, app: Application) -> float:
    if normalize_company(extraction.company) != normalize_company(app.company):
        return 0.0
    if extraction.posting_id and app.posting_id and extraction.posting_id == app.posting_id:
        return POSTING_ID_SCORE
    total = COMPANY_SCORE
    if extraction.role:
        a, b = role_tokens(extraction.role), role_tokens(app.role)
        overlap = len(a & b) / len(a | b) if a | b else 0.0
        total += ROLE_STRONG if overlap >= 0.6 else ROLE_WEAK if overlap >= 0.3 else 0.0
    return total


def match(extraction: Extraction, applications: list[Application]) -> Match:
    scored = sorted(((score(extraction, a), a) for a in applications), key=lambda p: p[0], reverse=True)
    scored = [(s, a) for s, a in scored if s > 0]
    candidates = tuple(a for _, a in scored)
    if not scored:
        return Match("none", None, (), "no_application_for_company")
    best_score, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if extraction.confidence < AUTO_MIN_CONFIDENCE:
        return Match("ask", None, candidates, "low_extraction_confidence")
    if best_score < AUTO_MIN_SCORE or round(best_score - runner_up, 6) < AUTO_MIN_GAP:
        return Match("ask", None, candidates, "ambiguous_application")
    return Match("auto", best, candidates, "unique_match")
