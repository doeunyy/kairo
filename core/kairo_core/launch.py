"""Start Hermes with only the settings it needs from ``kairo.env``.

Hermes gets Discord, Hermes and API-server variables. Mail, Notion and calendar secrets stay
out of its environment; the Kairo MCP server reads them from ``kairo.env`` itself.

    python -m kairo_core.launch gateway        # hermes gateway run
    python -m kairo_core.launch chat -Q ...    # any other hermes arguments
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .config import load_env

PASS_PREFIXES = ("DISCORD_", "HERMES_", "API_SERVER_")
DEFAULT_HERMES = "~/.local/share/kairo/hermes-agent/.venv/bin/hermes"


def hermes_env(values: dict[str, str], base: dict[str, str]) -> dict[str, str]:
    env = {k: v for k, v in base.items() if not k.startswith(("KAIRO_", "TAILSCALE_"))}
    env.update({k: os.path.expanduser(v) for k, v in values.items() if k.startswith(PASS_PREFIXES) and v})
    home = values.get("DISCORD_HOME_CHANNEL", "")
    if home:
        # Answer only in Kairo's own channel (and DMs), without needing an @mention there.
        env.setdefault("DISCORD_ALLOWED_CHANNELS", home)
        env.setdefault("DISCORD_FREE_RESPONSE_CHANNELS", home)
    return env


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv) or ["gateway"]
    if args[0] == "gateway":
        args = ["gateway", "run", *args[1:]]
    values = load_env()
    hermes = os.path.expanduser(values.get("KAIRO_HERMES_BIN") or DEFAULT_HERMES)
    if not Path(hermes).exists():
        sys.exit(f"Hermes not found at {hermes}; set KAIRO_HERMES_BIN")
    os.execve(hermes, [hermes, *args], hermes_env(values, dict(os.environ)))


if __name__ == "__main__":
    main()
