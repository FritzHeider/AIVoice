"""Transactional email templates in plain ``string.Template`` (jinja2 is for prompt templates, specs/02 §3).

Each template renders a plain-text and an HTML body. Every substituted value is HTML-escaped in the HTML
body, so a display name such as ``<script>`` arrives as text.
"""

from __future__ import annotations

import html
from string import Template
from typing import Final

from va_common.email.message import EmailMessage

__all__ = ["EMAIL_VERIFICATION_HOURS", "TEMPLATES", "verify_email"]

EMAIL_VERIFICATION_HOURS: Final = 24

_VERIFY_SUBJECT: Final = "Verify your VoiceAgents email address"

_VERIFY_TEXT: Final = Template(
    """Hi $name,

Confirm that this is your email address to finish setting up your VoiceAgents account:

$link

The link expires in $hours hours. If you did not create a VoiceAgents account, you can ignore this email; no
account will be activated without this confirmation.

The VoiceAgents team
"""
)

_VERIFY_HTML: Final = Template(
    """<!doctype html>
<html lang="en">
<body style="margin:0;padding:24px;background:#f6f5f2;color:#1d1c1a;font-family:Georgia,serif;">
<div style="max-width:560px;margin:0 auto;background:#ffffff;padding:32px;border-radius:8px;">
<p style="margin:0 0 16px;">Hi $name,</p>
<p style="margin:0 0 24px;">Confirm that this is your email address to finish setting up your VoiceAgents
account.</p>
<p style="margin:0 0 24px;"><a href="$link" style="display:inline-block;padding:12px 20px;background:#1d1c1a;
color:#ffffff;text-decoration:none;border-radius:6px;">Verify email address</a></p>
<p style="margin:0 0 16px;font-size:14px;">Or paste this link into your browser:<br>$link</p>
<p style="margin:0 0 16px;font-size:14px;">The link expires in $hours hours. If you did not create a
VoiceAgents account, you can ignore this email; no account will be activated without this confirmation.</p>
<p style="margin:0;font-size:14px;">The VoiceAgents team</p>
</div>
</body>
</html>
"""
)

TEMPLATES: Final[frozenset[str]] = frozenset({"verify_email"})


def verify_email(*, name: str, link: str, to: str) -> EmailMessage:
    """The signup verification email; ``link`` carries the one-time token and expires after 24 hours."""
    hours = str(EMAIL_VERIFICATION_HOURS)
    return EmailMessage(
        to=to,
        subject=_VERIFY_SUBJECT,
        text=_VERIFY_TEXT.substitute(name=name, link=link, hours=hours),
        html=_VERIFY_HTML.substitute(name=html.escape(name), link=html.escape(link), hours=hours),
        template="verify_email",
    )
