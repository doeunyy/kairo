"""Discord over REST only: alerts, draft previews, and reaction-based approval.

Hermes owns the bot's gateway connection for chat. This module needs no gateway: it posts
with the bot token and reads who reacted to a preview. Discord itself vouches for the
reacting user's numeric ID, which ``approval_sweep`` checks against the allowlist before
the outbox issues a token. Mentions are disabled on every post, so text quoted from an
email cannot ping anyone.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import Any

from .jsonhttp import JsonClient, Transport
from .outbox import ApprovalError, Draft, Outbox

API = "https://discord.com/api/v10"
APPROVE, DISCARD = "✅", "🗑️"
HASH_FOOTER = re.compile(r"sha256:([0-9a-f]{64})")
MAX_CONTENT = 2000
NO_MENTIONS = {"parse": []}


class Discord:
    def __init__(self, token: str, transport: Transport | None = None):
        self.client = JsonClient(API, {"Authorization": f"Bot {token}"}, transport=transport)

    def post(self, channel_id: str, content: str = "", embeds: list[dict] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"content": content[:MAX_CONTENT], "allowed_mentions": NO_MENTIONS}
        if embeds:
            body["embeds"] = embeds
        return self.client.request("POST", f"/channels/{channel_id}/messages", body)

    def get_message(self, channel_id: str, message_id: str) -> dict[str, Any]:
        return self.client.request("GET", f"/channels/{channel_id}/messages/{message_id}")

    def react(self, channel_id: str, message_id: str, emoji: str) -> None:
        self.client.request("PUT", f"/channels/{channel_id}/messages/{message_id}/reactions/"
                                   f"{urllib.parse.quote(emoji)}/@me")

    def reaction_users(self, channel_id: str, message_id: str, emoji: str) -> list[str]:
        users = self.client.request("GET", f"/channels/{channel_id}/messages/{message_id}/reactions/"
                                           f"{urllib.parse.quote(emoji)}?limit=100")
        return [u["id"] for u in users if not u.get("bot")]


def _quote(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def draft_preview_embed(draft: Draft) -> dict[str, Any]:
    return {
        "title": f"Email draft awaiting approval · {draft.draft_id}",
        "description": _quote(draft.body, 3500),
        "fields": [{"name": "To", "value": draft.to_addr, "inline": True},
                   {"name": "Subject", "value": _quote(draft.subject, 250) or "(no subject)", "inline": True}],
        "footer": {"text": f"{APPROVE} approve and send · {DISCARD} discard · ask in chat for changes · sha256:{draft.body_hash}"},
    }


def request_approval(discord: Discord, outbox: Outbox, draft: Draft, channel_id: str) -> str:
    message = discord.post(channel_id, embeds=[draft_preview_embed(draft)])
    outbox.set_preview(draft.draft_id, channel_id, message["id"])
    for emoji in (APPROVE, DISCARD):
        discord.react(channel_id, message["id"], emoji)
    return message["id"]


@dataclass(frozen=True)
class ApprovalResult:
    draft_id: str
    action: str          # sent | discarded | refused | waiting
    detail: str = ""


def approval_sweep(discord: Discord, outbox: Outbox, smtp) -> list[ApprovalResult]:
    """Act on reactions to pending previews. Called on a short timer by the watcher."""
    results = []
    for draft in outbox.awaiting_approval():
        channel, mid = draft.preview_channel, draft.preview_message
        approvers = [u for u in discord.reaction_users(channel, mid, APPROVE) if u in outbox.approvers]
        discarders = [u for u in discord.reaction_users(channel, mid, DISCARD) if u in outbox.approvers]
        if discarders:
            outbox.discard(draft.draft_id, discarders[0])
            discord.post(channel, f"{DISCARD} Discarded draft `{draft.draft_id}`.")
            results.append(ApprovalResult(draft.draft_id, "discarded"))
            continue
        if not approvers:
            results.append(ApprovalResult(draft.draft_id, "waiting"))
            continue
        # The hash the approver actually saw is the one in the preview on Discord.
        footer = (discord.get_message(channel, mid).get("embeds") or [{}])[0].get("footer", {}).get("text", "")
        shown = HASH_FOOTER.search(footer)
        try:
            token = outbox.approve(draft.draft_id, approvers[0], shown.group(1) if shown else "")
            sent_id = outbox.send(draft.draft_id, token, smtp)
        except ApprovalError as exc:
            outbox.block(draft.draft_id, str(exc))
            discord.post(channel, f"⚠️ Blocked sending draft `{draft.draft_id}`: {exc}")
            results.append(ApprovalResult(draft.draft_id, "refused", str(exc)))
            continue
        discord.post(channel, f"📨 Sent draft `{draft.draft_id}` to {draft.to_addr}.")
        results.append(ApprovalResult(draft.draft_id, "sent", sent_id))
    return results
