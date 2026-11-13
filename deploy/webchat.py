#!/usr/bin/env python3
"""Send one prompt to the deployed agent through its web chat API and record the result.

    deploy/webchat.py "What is due this week?"                 # prints the reply
    deploy/webchat.py --label W1 --out docs/evidence/security/webchat.jsonl "…"

The key is read from KAIRO_ENV_FILE (default ~/.a3-kairo/kairo.env) and never printed. The
same request path as the browser chat: POST /v1/chat/completions with the API_SERVER_KEY.
Each run appends {label, ts, prompt, status, reply, latency_s} to --out when given, so security
tests and benchmarks can be matched against the router's Cloud Logging lines by time.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

URL = os.environ.get("KAIRO_URL", "https://kairo-270801978750.us-west1.run.app")


def api_key() -> str:
    path = Path(os.environ.get("KAIRO_ENV_FILE", "~/.a3-kairo/kairo.env")).expanduser()
    for line in path.read_text().splitlines():
        if line.startswith("API_SERVER_KEY="):
            return line.split("=", 1)[1].split(" #")[0].strip().strip("\"'")
    raise SystemExit("API_SERVER_KEY not found in the env file")


def ask(prompt: str, key: str | None = None, timeout: float = 600) -> dict:
    body = json.dumps({"model": "hermes-agent", "messages": [{"role": "user", "content": prompt}]}).encode()
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(f"{URL}/v1/chat/completions", data=body, headers=headers)
    started = datetime.now(timezone.utc)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            status = resp.status
            reply = data["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as exc:
        status, reply = exc.code, exc.read().decode("utf-8", "replace")[:500]
    return {"ts": started.isoformat(timespec="seconds"), "prompt": prompt, "status": status,
            "reply": reply, "latency_s": round(time.monotonic() - t0, 2)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt")
    ap.add_argument("--label", default="")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--key", choices=["valid", "none", "wrong"], default="valid",
                    help="send the real key, no Authorization header, or a wrong key")
    a = ap.parse_args()
    key = {"valid": api_key(), "none": None, "wrong": "x" * 43}[a.key]
    row = {"label": a.label, "key": a.key, **ask(a.prompt, key)}
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        with a.out.open("a") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"[{row['status']}] {row['latency_s']}s\n{row['reply']}", file=sys.stdout)


if __name__ == "__main__":
    main()
