"""Copy proactive alerts into the test mailbox, under the Gmail label "Kairo Alerts".

Discord is Kairo's delivery channel. Graders sign in to the test Gmail account, not the private
Discord server, so each alert is also added to that label with IMAP APPEND. Nothing is sent: the
message is placed straight into the label, never into INBOX, so the mail watcher (which reads
INBOX only) never sees it, and no SMTP or recipient is involved. A failed copy is logged and
never blocks the Discord alert.
"""

from __future__ import annotations

import logging
import re
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Callable

log = logging.getLogger("kairo")
DEFAULT_LABEL = "Kairo Alerts"
ALERT_HEADER = "X-Kairo-Alert"
MARKDOWN = re.compile(r"\*\*|__|`|~~")       # bold, code, strike; single underscores stay (status_names)
FOOTER = ("\n\n--\nCopied from Kairo's Discord alerts so they can be checked without Discord. "
          "Kairo added this message to the label directly; it was not sent to anyone.")


def build_alert(address: str, text: str, kind: str) -> EmailMessage:
    first = MARKDOWN.sub("", text.strip().splitlines()[0] if text.strip() else kind)
    msg = EmailMessage()
    msg["From"], msg["To"] = f"Kairo <{address}>", address
    msg["Subject"] = "[Kairo] " + (first if len(first) <= 120 else first[:119] + "…")
    msg["Date"], msg["Message-ID"] = formatdate(localtime=False), make_msgid(domain="kairo.alerts")
    msg[ALERT_HEADER] = kind
    msg.set_content(MARKDOWN.sub("", text) + FOOTER)
    return msg


class AlertMirror:
    def __init__(self, mailbox_factory: Callable, address: str, label: str = DEFAULT_LABEL):
        self.mailbox_factory, self.address, self.label = mailbox_factory, address, label

    def __call__(self, text: str, kind: str = "alert") -> bool:
        if not text.strip():
            return False
        try:
            with self.mailbox_factory() as box:
                folder = box.ensure_folder(self.label)
                box.append(folder, build_alert(self.address, text, kind))   # unread, so it stands out
            return True
        except Exception as exc:
            log.warning("alert copy to Gmail failed: %s", type(exc).__name__)
            return False
