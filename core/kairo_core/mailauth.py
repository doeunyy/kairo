"""Read Gmail's verdict on who really sent a message (``Authentication-Results``, RFC 8601).

Gmail adds its own ``Authentication-Results: mx.google.com; ...`` header at the top of every
message it receives. Anything below it came with the message and may be forged, so only the
first header counts, and only when its authserv-id is Gmail's. A message without that header
did not come through Gmail's MX (for example a draft, or a message added with IMAP APPEND by
the account owner).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TRUSTED_AUTHSERV = "mx.google.com"
METHOD = re.compile(r"\b(dkim|spf|dmarc)\s*=\s*([a-z]+)", re.IGNORECASE)
PROP = re.compile(r"\b(header\.d|header\.i|header\.from|smtp\.mailfrom)\s*=\s*([^\s;()]+)", re.IGNORECASE)
COMMENT = re.compile(r"\([^()]*\)")


@dataclass(frozen=True)
class AuthResult:
    authserv: str
    dkim: str | None = None
    dkim_domain: str | None = None
    spf: str | None = None
    spf_domain: str | None = None
    dmarc: str | None = None
    dmarc_from: str | None = None


def _domain(value: str) -> str:
    value = value.strip().strip("<>").lower().lstrip("@")
    return value.rsplit("@", 1)[-1]


def parse_auth_results(headers: list[str]) -> AuthResult | None:
    """The topmost header if Gmail wrote it, else None. Only the first result per method is used."""
    if not headers:
        return None
    text = COMMENT.sub(" ", " ".join(str(headers[0]).split()))
    authserv, _, rest = text.partition(";")
    if authserv.strip().lower() != TRUSTED_AUTHSERV:
        return None
    found: dict[str, tuple[str, str | None]] = {}
    for clause in rest.split(";"):
        m = METHOD.search(clause)
        if not m or m.group(1).lower() in found:
            continue
        method = m.group(1).lower()
        props = {k.lower(): _domain(v) for k, v in PROP.findall(clause)}
        domain = {"dkim": props.get("header.d") or props.get("header.i"),
                  "spf": props.get("smtp.mailfrom"),
                  "dmarc": props.get("header.from")}[method]
        found[method] = (m.group(2).lower(), domain)
    get = lambda k: found.get(k, (None, None))
    return AuthResult(TRUSTED_AUTHSERV, *get("dkim"), *get("spf"), *get("dmarc"))


def aligned(a: str | None, b: str | None) -> bool:
    """Relaxed alignment: same domain, or one is a subdomain of the other."""
    if not a or not b:
        return False
    a, b = a.lower(), b.lower()
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def authenticated_domain(auth: AuthResult | None, from_domain: str) -> bool:
    """True when Gmail vouches that the From domain really sent it (DMARC, or aligned DKIM)."""
    if auth is None or not from_domain:
        return False
    if auth.dmarc == "fail":
        return False
    if auth.dmarc == "pass" and aligned(auth.dmarc_from or from_domain, from_domain):
        return True
    return auth.dkim == "pass" and aligned(auth.dkim_domain, from_domain)
