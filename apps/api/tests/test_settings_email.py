"""Email settings on the api (specs/02-repo-layout-and-conventions.md §6: ``SMTP_URL``, ``EMAIL_FROM``)."""

from __future__ import annotations

import pytest
from api_helpers import make_settings
from pydantic import ValidationError

from va_common.email import SmtpConfig


def test_api_settings_expose_smtp_config() -> None:
    settings = make_settings()
    assert settings.smtp_config() == SmtpConfig("smtp", "localhost", 1025, None, None, False, None)
    assert settings.email_from == "VoiceAgents <no-reply@voiceagents.local>"


def test_api_settings_read_smtp_url_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMTP_URL", "smtp+starttls://mailer:pw@smtp.example.com")
    config = make_settings().smtp_config()
    assert (config.hostname, config.port, config.start_tls) == ("smtp.example.com", 587, True)


def test_api_settings_refuse_plain_smtp_in_production() -> None:
    with pytest.raises(ValidationError, match="smtp://"):
        make_settings(
            va_env="production",
            smtp_url="smtp://smtp.example.com",
            kms_provider="aws",
            aws_kms_key_id="alias/voiceagents",
        )
