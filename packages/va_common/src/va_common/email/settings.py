"""Email settings ``SMTP_URL`` and ``EMAIL_FROM`` (specs/02-repo-layout-and-conventions.md §6).

Used by the api and the worker. ``SMTP_URL`` may carry relay credentials, so it is a secret
(specs/28-security.md §2). Only ``local`` and ``test`` accept the plain ``smtp://`` scheme.
"""

from __future__ import annotations

import email.utils

from pydantic import SecretStr, field_validator, model_validator

from va_common.email.config import PLAIN_SCHEME, SmtpConfig, parse_smtp_url
from va_common.settings import BaseServiceSettings, Environment

__all__ = ["EmailSettings"]

_PLAIN_ALLOWED: frozenset[Environment] = frozenset({Environment.LOCAL, Environment.TEST})


class EmailSettings(BaseServiceSettings):
    """Transactional email relay and sender address."""

    smtp_url: SecretStr = SecretStr("smtp://localhost:1025")
    email_from: str = "VoiceAgents <no-reply@voiceagents.local>"

    @field_validator("smtp_url")
    @classmethod
    def _check_smtp_url(cls, value: SecretStr) -> SecretStr:
        parse_smtp_url(value.get_secret_value())
        return value

    @field_validator("email_from")
    @classmethod
    def _check_email_from(cls, value: str) -> str:
        address = email.utils.parseaddr(value)[1]
        local, at, domain = address.rpartition("@")
        if not (at and local and domain):
            msg = "EMAIL_FROM must be an address or 'Name <address>'"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _check_plain_smtp(self) -> EmailSettings:
        if self.smtp_config().scheme == PLAIN_SCHEME and self.va_env not in _PLAIN_ALLOWED:
            msg = "SMTP_URL with smtp:// is allowed only when VA_ENV is local or test; use smtps:// or smtp+starttls://"
            raise ValueError(msg)
        return self

    def smtp_config(self) -> SmtpConfig:
        """The parsed relay configuration."""
        return parse_smtp_url(self.smtp_url.get_secret_value())
