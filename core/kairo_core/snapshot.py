"""State snapshots to the Cloud Storage mount, and the cloud login written back to Secret Manager.

SQLite does not work safely on Cloud Storage FUSE (no locks, no atomic rename), so live state
stays on the container's local disk and this module copies it to the bucket:

    python -m kairo_core.snapshot backup     # consistent tar.gz of Kairo state + HERMES_HOME
    python -m kairo_core.snapshot restore    # newest snapshot back to local disk (start-up)
    python -m kairo_core.snapshot sync-auth  # push a refreshed Hermes auth.json to Secret Manager

SQLite files are copied with the online backup API, so a snapshot taken while processes are
writing is still consistent. Secrets (approval key, auth.json, kairo.env) are never included.
"""

from __future__ import annotations

import base64
import hashlib

import json
import os
import sqlite3
import sys
import tarfile
import tempfile

import urllib.request
from datetime import datetime, timezone
from pathlib import Path

KEEP = 8
SKIP_NAMES = {"auth.json", "auth.lock", ".env", "kairo.env", "approval.key", "gateway.sock", "gateway.pid",
              "gateway.lock"}
SKIP_DIRS = {"secrets", "cache", "audio_cache", "image_cache", "sandboxes", "flags", "__pycache__", "bin"}
SQLITE_SUFFIXES = (".db", ".sqlite3")
SQLITE_SIDECARS = ("-wal", "-shm", "-journal")


def _event(event: str, **fields) -> None:
    print(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "component": "snapshot",
                      "event": event, **fields}), flush=True)


def _members(root: Path):
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts[:-1]) or rel.parts[0] in SKIP_DIRS:
            continue
        if path.is_dir() or path.is_symlink() or path.name in SKIP_NAMES or path.name.endswith(SQLITE_SIDECARS):
            continue
        if path.name.endswith((".lock", ".sock")):
            continue
        yield path, rel


def _add_sqlite(tar: tarfile.TarFile, path: Path, arcname: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "copy.db"
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        dst = sqlite3.connect(copy)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        tar.add(copy, arcname=arcname)


def backup(roots: dict[str, Path], dest: Path) -> Path:
    """Write ``snap-<UTC>.tar.gz`` into ``dest`` (the bucket mount) and keep the newest KEEP."""
    dest.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / f"snap-{stamp}.tar.gz"
        count = 0
        with tarfile.open(local, "w:gz") as tar:
            for label, root in roots.items():
                if not root.exists():
                    continue
                for path, rel in _members(root):
                    arcname = f"{label}/{rel}"
                    try:
                        if path.suffix in SQLITE_SUFFIXES:
                            _add_sqlite(tar, path, arcname)
                        else:
                            tar.add(path, arcname=arcname)
                        count += 1
                    except (OSError, sqlite3.Error) as exc:
                        _event("skip_file", path=arcname, error=type(exc).__name__)
        # One sequential write; Cloud Storage objects appear whole or not at all.
        target = dest / local.name
        with open(local, "rb") as src, open(target, "wb") as out:
            while chunk := src.read(1 << 20):
                out.write(chunk)
        os.chmod(target, 0o600)
    for old in sorted(dest.glob("snap-*.tar.gz"))[:-KEEP]:
        old.unlink(missing_ok=True)
    _event("backup_ok", file=target.name, files=count, bytes=target.stat().st_size)
    return target


def restore(roots: dict[str, Path], src: Path) -> str | None:
    snaps = sorted(src.glob("snap-*.tar.gz")) if src.exists() else []
    if not snaps:
        _event("restore_skipped", reason="no_snapshot")
        return None
    latest = snaps[-1]
    with tarfile.open(latest, "r:gz") as tar:
        for member in tar.getmembers():
            label, _, rel = member.name.partition("/")
            if label not in roots or not member.isfile():
                continue
            root = roots[label].resolve()
            target = (root / rel).resolve()
            if root not in target.parents:          # refuse absolute paths and ../ escapes
                _event("restore_skip", path=member.name, reason="outside_root")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as fh:
                target.write_bytes(fh.read())
            os.chmod(target, 0o600)
    _event("restore_ok", file=latest.name)
    return latest.name


# ── Secret Manager write-back for the refreshed ChatGPT login ─────────────────────────

METADATA_TOKEN = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"


def _token() -> str:
    req = urllib.request.Request(METADATA_TOKEN, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())["access_token"]


def sync_auth(auth_file: Path, state_file: Path, project: str, secret: str) -> bool:
    """If Hermes refreshed auth.json, add it as the secret's newest version (used on next start)."""
    if not auth_file.exists():
        return False
    data = auth_file.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if state_file.exists() and state_file.read_text().strip() == digest:
        return False
    url = f"https://secretmanager.googleapis.com/v1/projects/{project}/secrets/{secret}:addVersion"
    body = json.dumps({"payload": {"data": base64.b64encode(data).decode()}}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        version = json.loads(resp.read())["name"].rsplit("/", 1)[-1]
    state_file.write_text(digest)
    _event("auth_synced", secret=secret, version=version)
    return True


def _roots() -> dict[str, Path]:
    return {"kairo": Path(os.environ.get("KAIRO_STATE_DIR", "state")),
            "hermes": Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()}


def main(argv: list[str] | None = None) -> int:
    cmd = (argv or sys.argv[1:] or [""])[0]
    bucket = Path(os.environ.get("KAIRO_SNAPSHOT_DIR", "/mnt/agent-state/snapshots"))
    if cmd == "backup":
        backup(_roots(), bucket)
    elif cmd == "restore":
        restore(_roots(), bucket)
    elif cmd == "sync-auth":
        home = _roots()["hermes"]
        sync_auth(home / "auth.json", home / ".auth.synced", os.environ["GOOGLE_CLOUD_PROJECT"],
                  os.environ.get("KAIRO_AUTH_SECRET", "kairo-chatgpt-auth"))
    else:
        print("usage: python -m kairo_core.snapshot backup|restore|sync-auth", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
