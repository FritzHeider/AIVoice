"""Transactional email templates in plain ``string.Template`` (jinja2 is for prompt templates, specs/02 §3).

Each template renders a plain-text and an HTML body from one shared layout: a greeting, a sentence explaining
the email, the link (a button in HTML), a notice about expiry and what to do if the email was unexpected, and
the sign-off. Every substituted value is HTML-escaped in the HTML body, so a display name such as
``<script>`` arrives as text.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from string import Template
from typing import Final

from va_common.email.message import EmailMessage

__all__ = [
    "EMAIL_VERIFICATION_HOURS",
    "PASSWORD_RESET_MINUTES",
    "TEMPLATES",
    "password_reset",
    "verify_email",
]

EMAIL_VERIFICATION_HOURS: Final = 24
PASSWORD_RESET_MINUTES: Final = 60

_TEXT_LAYOUT: Final = Template(
    """Hi $name,

$intro:

$link

$notice

The VoiceAgents team
"""
)

_HTML_LAYOUT: Final = Template(
    """<!doctype html>
<html lang="en">
<body style="margin:0;padding:24px;background:#f6f5f2;color:#1d1c1a;font-family:Georgia,serif;">
<div style="max-width:560px;margin:0 auto;background:#ffffff;padding:32px;border-radius:8px;">
<p style="margin:0 0 16px;">Hi $name,</p>
<p style="margin:0 0 24px;">$intro.</p>
<p style="margin:0 0 24px;"><a href="$link" style="display:inline-block;padding:12px 20px;background:#1d1c1a;
color:#ffffff;text-decoration:none;border-radius:6px;">$button</a></p>
<p style="margin:0 0 16px;font-size:14px;">Or paste this link into your browser:<br>$link</p>
<p style="margin:0 0 16px;font-size:14px;">$notice</p>
<p style="margin:0;font-size:14px;">The VoiceAgents team</p>
</div>
</body>
</html>
"""
)


@dataclass(frozen=True, slots=True)
class _Copy:
    """The fixed wording of one template; ``intro`` and ``notice`` hold the line breaks of each body."""

    template: str
    subject: str
    button: str
    text_intro: str
    html_intro: str
    text_notice: str
    html_notice: str

    def render(self, *, name: str, link: str, to: str) -> EmailMessage:
        return EmailMessage(
            to=to,
            subject=self.subject,
            text=_TEXT_LAYOUT.substitute(
                name=name, intro=self.text_intro, link=link, notice=self.text_notice
            ),
            html=_HTML_LAYOUT.substitute(
                name=html.escape(name),
                intro=self.html_intro,
                link=html.escape(link),
                button=self.button,
                notice=self.html_notice,
            ),
            template=self.template,
        )


_VERIFY: Final = _Copy(
    template="verify_email",
    subject="Verify your VoiceAgents email address",
    button="Verify email address",
    text_intro="Confirm that this is your email address to finish setting up your VoiceAgents account",
    html_intro="Confirm that this is your email address to finish setting up your VoiceAgents\naccount",
    text_notice=(
        f"The link expires in {EMAIL_VERIFICATION_HOURS} hours. If you did not create a VoiceAgents account, "
        "you can ignore this email; no\naccount will be activated without this confirmation."
    ),
    html_notice=(
        f"The link expires in {EMAIL_VERIFICATION_HOURS} hours. If you did not create a\nVoiceAgents "
        "account, you can ignore this email; no account will be activated without this confirmation."
    ),
)

_RESET: Final = _Copy(
    template="password_reset",
    subject="Reset your VoiceAgents password",
    button="Choose a new password",
    text_intro="Someone asked to reset the password of your VoiceAgents account. Choose a new one here",
    html_intro="Someone asked to reset the password of your VoiceAgents account. Choose a new one\nhere",
    text_notice=(
        f"The link expires in {PASSWORD_RESET_MINUTES} minutes and works once. If you did not ask for this, "
        "you can ignore\nthis email; your password stays the same."
    ),
    html_notice=(
        f"The link expires in {PASSWORD_RESET_MINUTES} minutes and works once. If you did not ask for\nthis, "
        "you can ignore this email; your password stays the same."
    ),
)

TEMPLATES: Final[frozenset[str]] = frozenset({_VERIFY.template, _RESET.template})


def verify_email(*, name: str, link: str, to: str) -> EmailMessage:
    """The signup verification email; ``link`` carries the one-time token and expires after 24 hours."""
    return _VERIFY.render(name=name, link=link, to=to)


def password_reset(*, name: str, link: str, to: str) -> EmailMessage:
    """The password reset email; ``link`` carries the one-time token and expires after 60 minutes."""
    return _RESET.render(name=name, link=link, to=to)
