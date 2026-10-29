"""Reset links, the reset email send, and the recent-auth rule of password change (specs/04 §4.1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from structlog.testing import capture_logs

from va_api.auth.password_reset import (
    REAUTH_MESSAGE,
    REAUTH_WINDOW,
    RESET_HOURLY_LIMIT,
    RESET_LIMIT_WINDOW,
    require_recent_auth,
    reset_link,
    send_password_reset,
)
from va_api.auth.sessions import ActiveSession
from va_common.email import EmailDeliveryError, EmailMessage, InMemoryEmailSender
from va_common.errors import AppError
from va_common.ids import new_id

TOKEN = "Xb4LZ0c9qkV2mR7tWn1sYe8pHu3aGj6dKf5oQi2NwTs"  # noqa: S105 - test token
CREATED = datetime(2026, 10, 29, 9, 30, tzinfo=UTC)


class FailingSender:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def send(self, message: EmailMessage) -> None:
        raise self.error from ValueError(message.template)


def _session(mfa_passed_at: datetime | None = None) -> ActiveSession:
    return ActiveSession(
        session_id=new_id(),
        token_hash="0" * 64,
        user_id=new_id(),
        active_org_id=None,
        mfa_passed=mfa_passed_at is not None,
        mfa_passed_at=mfa_passed_at,
        created_at=CREATED,
        expires_at=CREATED + timedelta(days=14),
    )


def test_constants_match_spec() -> None:
    assert RESET_HOURLY_LIMIT == 3
    assert timedelta(hours=1) == RESET_LIMIT_WINDOW
    assert timedelta(minutes=10) == REAUTH_WINDOW


@pytest.mark.parametrize("base", ["https://app.voiceagents.test", "https://app.voiceagents.test/"])
def test_reset_link_joins_without_double_slash(base: str) -> None:
    assert reset_link(base, TOKEN) == f"https://app.voiceagents.test/reset-password/{TOKEN}"


def test_recent_auth_boundary() -> None:
    active = _session()
    require_recent_auth(active, CREATED)
    require_recent_auth(active, CREATED + REAUTH_WINDOW - timedelta(microseconds=1))
    for late in (CREATED + REAUTH_WINDOW, CREATED + timedelta(days=1)):
        with pytest.raises(AppError) as excinfo:
            require_recent_auth(active, late)
        assert (excinfo.value.code, excinfo.value.http_status) == ("REAUTH_REQUIRED", 401)
        assert excinfo.value.message == REAUTH_MESSAGE


def test_recent_auth_reports_the_caller_message() -> None:
    with pytest.raises(AppError) as excinfo:
        require_recent_auth(
            _session(), CREATED + REAUTH_WINDOW, message="Sign in again to transfer ownership."
        )
    assert (excinfo.value.code, excinfo.value.message) == (
        "REAUTH_REQUIRED",
        "Sign in again to transfer ownership.",
    )


def test_recent_auth_counts_from_the_last_mfa_verification() -> None:
    verified = CREATED + timedelta(hours=1)
    active = _session(mfa_passed_at=verified)
    require_recent_auth(active, verified + timedelta(minutes=9))
    with pytest.raises(AppError) as excinfo:
        require_recent_auth(active, verified + REAUTH_WINDOW)
    assert excinfo.value.code == "REAUTH_REQUIRED"


def test_recent_auth_ignores_an_mfa_time_before_creation() -> None:
    active = _session(mfa_passed_at=CREATED - timedelta(hours=1))
    require_recent_auth(active, CREATED + timedelta(minutes=9))


async def test_send_password_reset_renders_template() -> None:
    sender = InMemoryEmailSender()
    await send_password_reset(
        sender, to="jane@acme-dental.test", name="Jane", token=TOKEN, dashboard_base_url="https://app.test/"
    )
    (message,) = sender.sent
    assert message.template == "password_reset"
    assert message.to == "jane@acme-dental.test"
    assert f"https://app.test/reset-password/{TOKEN}" in message.text


@pytest.mark.parametrize(
    "error", [EmailDeliveryError("delivery of 'password_reset' failed"), OSError("down")]
)
async def test_send_password_reset_failure_is_logged_without_address(error: Exception) -> None:
    with capture_logs() as logs:
        await send_password_reset(
            FailingSender(error),
            to="jane.private@acme-dental.test",
            name="Jane",
            token=TOKEN,
            dashboard_base_url="https://app.test",
        )
    (entry,) = logs
    assert entry["event"] == "password_reset_email_failed"
    assert entry["template"] == "password_reset"
    assert entry["error_type"] == type(error).__name__
    assert "jane.private" not in repr(entry)
    assert TOKEN not in repr(entry)
