"""Runtime settings from ``kairo.env`` (laptop) or the process environment (cloud).

Secret values are never logged: use ``Settings.redact`` on any text that might echo one.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .prefilter import Allowlist

DEFAULT_ENV_FILE = Path("~/.a3-kairo/kairo.env").expanduser()
SECRET_KEYS = ("KAIRO_MAIL_APP_PASSWORD", "KAIRO_CALENDAR_ICS_URL", "KAIRO_NOTION_TOKEN",
               "DISCORD_BOT_TOKEN", "API_SERVER_KEY", "TAILSCALE_AUTHKEY", "KAIRO_APPROVAL_KEY")
LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


def _read_with_retry(path: Path, attempts: int = 5, delay: float = 1.0) -> str:
    """Secret Manager files can return EIO for a few seconds after Cloud Run resumes from
    scheduled maintenance; retry instead of failing the whole process."""
    for attempt in range(attempts):
        try:
            return path.read_text()
        except OSError:
            if attempt == attempts - 1 or not path.exists():
                raise
            time.sleep(delay * (attempt + 1))
    raise AssertionError("unreachable")


def read_env_file(path: Path | str) -> dict[str, str]:
    """Parse KEY=value lines. Unquoted values lose a trailing `` # comment``."""
    values: dict[str, str] = {}
    for line in _read_with_retry(Path(path)).splitlines():
        m = LINE.match(line.strip())
        if not m:
            continue
        value = m.group(2).strip()
        if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        else:
            value = re.sub(r"\s+#.*$", "", value).strip()
        values[m.group(1)] = value
    return values


def load_env(path: Path | str | None = None) -> dict[str, str]:
    """File values first, then real environment variables on top (the cloud path)."""
    env_path = Path(path or os.environ.get("KAIRO_ENV_FILE", DEFAULT_ENV_FILE)).expanduser()
    values = read_env_file(env_path) if env_path.exists() else {}
    values.update({k: v for k, v in os.environ.items() if k.startswith(("KAIRO_", "DISCORD_", "A3_"))})
    return values


def parse_allowlist(raw: str) -> Allowlist:
    """``a@x.test, @y.test`` → address a@x.test and the whole domain y.test."""
    addresses, domains = set(), set()
    for item in (part.strip().lower() for part in raw.split(",")):
        if not item:
            continue
        if item.startswith("@"):
            domains.add(item[1:])
        elif "@" in item:
            addresses.add(item)
        else:
            domains.add(item)
    return Allowlist(frozenset(addresses), frozenset(domains))


@dataclass(frozen=True)
class Settings:
    env: dict[str, str]

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Settings":
        return cls(load_env(path))

    def get(self, key: str, default: str = "") -> str:
        return self.env.get(key) or default

    def require(self, key: str) -> str:
        value = self.env.get(key, "")
        if not value:
            raise RuntimeError(f"{key} is not set in kairo.env")
        return value

    @property
    def state_dir(self) -> Path:
        path = Path(self.get("KAIRO_STATE_DIR") or self.get("A3_STATE_DIR", "state")).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def sender_allowlist(self) -> Allowlist:
        return parse_allowlist(self.get("KAIRO_SENDER_ALLOWLIST"))

    @property
    def recipient_allowlist(self) -> Allowlist:
        return parse_allowlist(self.get("KAIRO_RECIPIENT_ALLOWLIST"))

    @property
    def allowed_discord_users(self) -> frozenset[str]:
        return frozenset(u.strip() for u in self.get("DISCORD_ALLOWED_USERS").split(",") if u.strip())

    def redact(self, text: object) -> str:
        out = str(text)
        for key in SECRET_KEYS:
            value = self.env.get(key, "")
            if len(value) > 4:
                out = out.replace(value, f"<{key}>")
        return out
