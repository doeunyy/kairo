"""Application statuses and the transitions an email is allowed to cause."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Status(str, Enum):
    INTERESTED = "Interested"
    PREPARING = "Preparing"
    APPLIED = "Applied"
    ASSESSMENT = "Assessment"
    INTERVIEW = "Interview"
    OFFER = "Offer"
    REJECTED = "Rejected"
    WITHDRAWN = "Withdrawn"


class MailType(str, Enum):
    ACK = "ack"                                   # application received
    ASSESSMENT = "assessment"                     # take-home / online test
    INTERVIEW_REQUEST = "interview_request"       # asks the candidate to pick a time
    INTERVIEW_CONFIRMED = "interview_confirmed"   # time is fixed
    REJECTION = "rejection"
    OFFER = "offer"
    NEWSLETTER = "newsletter"
    OTHER = "other"


S = Status
# Forward progress only. INTERESTED and PREPARING are set by the user or by JD analysis,
# never reached from an email. Terminal states accept nothing from email.
ALLOWED: dict[Status, frozenset[Status]] = {
    S.INTERESTED: frozenset({S.APPLIED}),
    S.PREPARING: frozenset({S.APPLIED}),
    S.APPLIED: frozenset({S.ASSESSMENT, S.INTERVIEW, S.OFFER, S.REJECTED}),
    S.ASSESSMENT: frozenset({S.INTERVIEW, S.OFFER, S.REJECTED}),
    S.INTERVIEW: frozenset({S.OFFER, S.REJECTED}),
    S.OFFER: frozenset(),
    S.REJECTED: frozenset(),
    S.WITHDRAWN: frozenset(),
}

TARGET: dict[MailType, Status] = {
    MailType.ACK: S.APPLIED,
    MailType.ASSESSMENT: S.ASSESSMENT,
    MailType.INTERVIEW_REQUEST: S.INTERVIEW,
    MailType.INTERVIEW_CONFIRMED: S.INTERVIEW,
    MailType.REJECTION: S.REJECTED,
    MailType.OFFER: S.OFFER,
}


@dataclass(frozen=True)
class Transition:
    result: str            # apply | same | ignore | refuse
    target: Status | None
    reason: str


def transition(current: Status, mail: MailType) -> Transition:
    target = TARGET.get(mail)
    if target is None:
        return Transition("ignore", None, f"no_status_for_{mail.value}")
    if target == current:
        # A second interview round or a reminder: refresh dates, keep the status.
        return Transition("same", current, "already_in_status")
    if target in ALLOWED[current]:
        return Transition("apply", target, "allowed")
    return Transition("refuse", target, f"{current.name}_to_{target.name}_not_allowed")
