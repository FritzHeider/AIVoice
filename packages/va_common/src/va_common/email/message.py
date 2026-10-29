"""Transactional email messages and their MIME rendering (stdlib ``email``, no template engine)."""

from __future__ import annotations

import email.message
import email.utils
from dataclasses import dataclass
from datetime import datetime

__all__ = ["EmailMessage", "to_mime"]


@dataclass(frozen=True, slots=True)
class EmailMessage:
    """One rendered email: recipient, subject, plain-text and HTML bodies, and the template it came from."""

    to: str
    subject: str
    text: str
    html: str
    template: str


def to_mime(message: EmailMessage, *, sender: str, now: datetime) -> email.message.EmailMessage:
    """A ``multipart/alternative`` message (text first, HTML preferred) with From/To/Subject/Date/Message-ID.

    ``sender`` is the ``EMAIL_FROM`` value (``Name <address>``); its domain scopes the ``Message-ID``.
    """
    domain = email.utils.parseaddr(sender)[1].rpartition("@")[2] or None
    mime = email.message.EmailMessage()
    mime["From"] = sender
    mime["To"] = message.to
    mime["Subject"] = message.subject
    mime["Date"] = email.utils.format_datetime(now)
    mime["Message-ID"] = email.utils.make_msgid(domain=domain)
    mime.set_content(message.text)
    mime.add_alternative(message.html, subtype="html")
    return mime
