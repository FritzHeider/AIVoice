"""Verification links, the resend limiter, and the verification email send (specs/04 §4.1)."""

from __future__ import annotations

import fakeredis
import pytest
from structlog.testing import capture_logs

from va_api.auth.login_throttle import locked_message, seconds_phrase
from va_api.auth.verification import (
    RESEND_LIMIT,
    RESEND_WINDOW_S,
    ResendLimiter,
    resend_key,
    resend_limited_message,
    send_verification,
    verify_link,
)
from va_common.email import EmailDeliveryError, EmailMessage, InMemoryEmailSender
from va_common.errors import AppError
from va_common.ids import new_id

TOKEN = "Xb4LZ0c9qkV2mR7tWn1sYe8pHu3aGj6dKf5oQi2NwTs"  # noqa: S105 - test token


class FailingSender:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def send(self, message: EmailMessage) -> None:
        raise self.error from ValueError(message.template)


@pytest.mark.parametrize("base", ["https://app.voiceagents.test", "https://app.voiceagents.test/"])
def test_verify_link_joins_without_double_slash(base: str) -> None:
    assert verify_link(base, TOKEN) == f"https://app.voiceagents.test/verify-email/{TOKEN}"


def test_resend_key_and_messages() -> None:
    user_id = new_id()
    assert resend_key(user_id) == f"auth:verify:resend:{user_id}"
    assert resend_limited_message(1) == "Too many verification emails; try again in 1 second."
    assert resend_limited_message(3600) == "Too many verification emails; try again in 3600 seconds."
    assert seconds_phrase(2) == "2 seconds"
    assert locked_message(1) == "Too many failed login attempts; try again in 1 second."


async def test_limiter_allows_three_per_window_then_rejects() -> None:
    redis = fakeredis.FakeAsyncRedis()
    limiter = ResendLimiter(redis)
    user_id = new_id()
    for _ in range(RESEND_LIMIT):
        await limiter.hit(user_id)
    ttl = await redis.ttl(resend_key(user_id))
    assert 0 < ttl <= RESEND_WINDOW_S
    with pytest.raises(AppError) as excinfo:
        await limiter.hit(user_id)
    error = excinfo.value
    assert error.code == "RATE_LIMITED"
    retry_after = int(error.headers["Retry-After"])
    assert 0 < retry_after <= RESEND_WINDOW_S
    assert error.message == resend_limited_message(retry_after)
    assert int(await redis.get(resend_key(user_id)) or 0) == RESEND_LIMIT + 1
    other = new_id()
    await limiter.hit(other)


async def test_limiter_without_ttl_reports_full_window() -> None:
    redis = fakeredis.FakeAsyncRedis()
    user_id = new_id()
    await redis.set(resend_key(user_id), RESEND_LIMIT)
    await redis.persist(resend_key(user_id))
    with pytest.raises(AppError) as excinfo:
        await ResendLimiter(redis).hit(user_id)
    assert excinfo.value.headers["Retry-After"] == str(RESEND_WINDOW_S)


async def test_limiter_allows_when_redis_is_down() -> None:
    server = fakeredis.FakeServer()
    server.connected = False
    limiter = ResendLimiter(fakeredis.FakeAsyncRedis(server=server))
    user_id = new_id()
    with capture_logs() as logs:
        for _ in range(RESEND_LIMIT + 2):
            await limiter.hit(user_id)
    assert {entry["event"] for entry in logs} == {"resend_limit_unavailable"}


async def test_send_verification_renders_template() -> None:
    sender = InMemoryEmailSender()
    await send_verification(
        sender, to="jane@acme-dental.test", name="Jane", token=TOKEN, dashboard_base_url="https://app.test/"
    )
    (message,) = sender.sent
    assert message.template == "verify_email"
    assert message.to == "jane@acme-dental.test"
    assert f"https://app.test/verify-email/{TOKEN}" in message.text


@pytest.mark.parametrize("error", [EmailDeliveryError("delivery of 'verify_email' failed"), OSError("down")])
async def test_send_verification_failure_is_logged_without_address(error: Exception) -> None:
    with capture_logs() as logs:
        await send_verification(
            FailingSender(error),
            to="jane.private@acme-dental.test",
            name="Jane",
            token=TOKEN,
            dashboard_base_url="https://app.test",
        )
    (entry,) = logs
    assert entry["event"] == "verification_email_failed"
    assert entry["template"] == "verify_email"
    assert entry["error_type"] == type(error).__name__
    assert "jane.private" not in repr(entry)
    assert TOKEN not in repr(entry)
