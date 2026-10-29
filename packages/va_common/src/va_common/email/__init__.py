"""Transactional email: messages, templates, and the ``EmailSender`` interface (SMTP and in-memory)."""

from __future__ import annotations

from va_common.email.config import SmtpConfig, parse_smtp_url
from va_common.email.message import EmailMessage, to_mime
from va_common.email.sender import (
    SMTP_TIMEOUT_S,
    EmailDeliveryError,
    EmailSender,
    InMemoryEmailSender,
    SmtpEmailSender,
)
from va_common.email.settings import EmailSettings
from va_common.email.templates import TEMPLATES, agent_key_request, invitation, password_reset, verify_email

__all__ = [
    "SMTP_TIMEOUT_S",
    "TEMPLATES",
    "EmailDeliveryError",
    "EmailMessage",
    "EmailSender",
    "EmailSettings",
    "InMemoryEmailSender",
    "SmtpConfig",
    "SmtpEmailSender",
    "agent_key_request",
    "invitation",
    "parse_smtp_url",
    "password_reset",
    "to_mime",
    "verify_email",
]
