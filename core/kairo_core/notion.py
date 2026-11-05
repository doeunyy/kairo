"""Application tracker in a dedicated Notion workspace.

Kairo owns one database under the shared parent page. Every write is version-aware:
``apply_update`` re-reads the page and refuses if ``agent_rev`` or Notion's
``last_edited_time`` moved since the plan was made (another writer or a manual edit),
so a stale plan never overwrites newer data. Notion rounds ``last_edited_time`` to the
minute, so a manual edit in the same minute as the read can slip past that half of the check.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .jsonhttp import JsonClient, Transport
from .matcher import Application
from .pipeline import Update
from .status import Status

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
DATABASE_TITLE = "Kairo Applications"
MAX_TEXT = 2000

# field name used in code → (Notion property name, type)
PROPS: dict[str, tuple[str, str]] = {
    "company": ("Company", "title"),
    "role": ("Role", "rich_text"),
    "posting_id": ("Posting ID", "rich_text"),
    "posting_url": ("Posting URL", "url"),
    "status": ("Status", "select"),
    "applied_on": ("Applied On", "date"),
    "applied_on_source": ("Applied Date Source", "select"),
    "last_contact": ("Last Contact", "date"),
    "next_action": ("Next Action", "rich_text"),
    "due": ("Due", "date"),
    "evidence_message_id": ("Evidence Message-ID", "rich_text"),
    "change_reason": ("Change Reason", "rich_text"),
    "jd_summary": ("JD Summary", "rich_text"),
    "agent_rev": ("agent_rev", "number"),
}
STATUS_COLORS = {
    Status.INTERESTED: "gray", Status.PREPARING: "brown", Status.APPLIED: "blue", Status.ASSESSMENT: "purple",
    Status.INTERVIEW: "orange", Status.OFFER: "green", Status.REJECTED: "red", Status.WITHDRAWN: "default",
}


class RevConflict(RuntimeError):
    pass


def _schema() -> dict[str, Any]:
    schema: dict[str, Any] = {}
    for field, (name, kind) in PROPS.items():
        if kind == "select" and field == "status":
            schema[name] = {"select": {"options": [{"name": s.value, "color": STATUS_COLORS[s]} for s in Status]}}
        elif kind == "select":
            schema[name] = {"select": {"options": [{"name": n} for n in ("email", "received", "manual")]}}
        elif kind == "number":
            schema[name] = {"number": {"format": "number"}}
        else:
            schema[name] = {kind: {}}
    return schema


def _text(value: str) -> list[dict]:
    return [{"type": "text", "text": {"content": value[:MAX_TEXT]}}] if value else []


def encode(field: str, value: Any) -> dict[str, Any]:
    _, kind = PROPS[field]
    if kind in ("title", "rich_text"):
        return {kind: _text("" if value is None else str(value))}
    if kind == "select":
        return {"select": None if value is None else {"name": str(value)}}
    if kind == "date":
        return {"date": None if value is None else {"start": value if isinstance(value, str) else value.isoformat()}}
    if kind == "url":
        return {"url": value or None}
    if kind == "number":
        return {"number": value}
    raise ValueError(kind)


def decode(prop: dict[str, Any]) -> Any:
    kind = prop.get("type")
    if kind in ("title", "rich_text"):
        return "".join(part.get("plain_text", "") for part in prop.get(kind) or []) or None
    if kind == "select":
        return (prop.get("select") or {}).get("name")
    if kind == "date":
        return (prop.get("date") or {}).get("start")
    if kind in ("url", "number"):
        return prop.get(kind)
    return None


@dataclass(frozen=True)
class Row:
    application: Application
    fields: dict[str, Any]


class NotionTracker:
    def __init__(self, token: str, parent_page_id: str, state_file: Path, transport: Transport | None = None):
        self.client = JsonClient(API, {"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION},
                                 transport=transport)
        self.parent = parent_page_id.replace("-", "")
        self.state_file = state_file
        self.database_id: str | None = None

    # ── database ────────────────────────────────────────────────────────────────────
    def ensure_database(self) -> str:
        """Find Kairo's database (cached id, then the parent page's children) or create it,
        then add any property a newer schema introduced."""
        db_id = self._cached_id() or self._find_child_database()
        if db_id is None:
            created = self.client.request("POST", "/databases", {
                "parent": {"type": "page_id", "page_id": self.parent},
                "title": _text(DATABASE_TITLE),
                "properties": _schema(),
            })
            db_id = created["id"]
        else:
            existing = self.client.request("GET", f"/databases/{db_id}")["properties"]
            missing = {k: v for k, v in _schema().items() if k not in existing}
            if missing:
                self.client.request("PATCH", f"/databases/{db_id}", {"properties": missing})
        self.database_id = db_id
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps({"database_id": db_id}))
        return db_id

    def _cached_id(self) -> str | None:
        if not self.state_file.exists():
            return None
        db_id = json.loads(self.state_file.read_text()).get("database_id")
        try:
            db = self.client.request("GET", f"/databases/{db_id}")
        except Exception:
            return None
        return None if db.get("archived") or db.get("in_trash") else db_id

    def _find_child_database(self) -> str | None:
        cursor = None
        while True:
            query = "?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
            page = self.client.request("GET", f"/blocks/{self.parent}/children{query}")
            for block in page["results"]:
                if block.get("type") == "child_database" and \
                        block["child_database"].get("title") == DATABASE_TITLE and not block.get("archived"):
                    return block["id"]
            if not page.get("has_more"):
                return None
            cursor = page["next_cursor"]

    def _db(self) -> str:
        return self.database_id or self.ensure_database()

    # ── rows ────────────────────────────────────────────────────────────────────────
    def _row(self, page: dict[str, Any]) -> Row | None:
        props = page["properties"]
        fields = {field: decode(props[name]) for field, (name, _) in PROPS.items() if name in props}
        try:
            status = Status(fields.get("status"))
        except ValueError:
            return None          # a status Kairo does not know: leave the row to the user
        applied = fields.get("applied_on")
        app = Application(
            id=page["id"], company=fields.get("company") or "", role=fields.get("role") or "", status=status,
            posting_id=fields.get("posting_id"), applied_on=date.fromisoformat(applied[:10]) if applied else None,
            rev=int(fields.get("agent_rev") or 0), edited_at=page.get("last_edited_time"))
        return Row(app, fields)

    def rows(self) -> list[Row]:
        out, cursor = [], None
        while True:
            body: dict[str, Any] = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            page = self.client.request("POST", f"/databases/{self._db()}/query", body)
            out += [r for r in (self._row(p) for p in page["results"] if not p.get("archived")) if r]
            if not page.get("has_more"):
                return out
            cursor = page["next_cursor"]

    def applications(self) -> list[Application]:
        return [r.application for r in self.rows()]

    def create_application(self, company: str, role: str, status: Status, **fields: Any) -> Application:
        values = {"company": company, "role": role, "status": status.value, "agent_rev": 0, **fields}
        page = self.client.request("POST", "/pages", {
            "parent": {"database_id": self._db()},
            "properties": {PROPS[k][0]: encode(k, v) for k, v in values.items()},
        })
        return self._row(page).application

    def apply_update(self, update: Update, reason: str) -> Application:
        page = self.client.request("GET", f"/pages/{update.application_id}")
        current = self._row(page)
        if current is None:
            raise RevConflict("row has a status Kairo does not manage")
        if current.application.rev != update.expected_rev:
            raise RevConflict(f"agent_rev is {current.application.rev}, plan expected {update.expected_rev}")
        if update.expected_edited_at and page.get("last_edited_time") != update.expected_edited_at:
            raise RevConflict("row was edited after the plan was made")
        values = {**update.fields, "change_reason": reason, "agent_rev": update.expected_rev + 1}
        updated = self.client.request("PATCH", f"/pages/{update.application_id}", {
            "properties": {PROPS[k][0]: encode(k, v) for k, v in values.items() if k in PROPS},
        })
        return self._row(updated).application
