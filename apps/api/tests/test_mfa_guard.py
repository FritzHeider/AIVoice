"""MFA failure lock and TOTP replay guard on fake Redis (specs/04-auth-tenancy-rbac.md §4.5)."""

from __future__ import annotations

import fakeredis
import pytest
from redis.asyncio import Redis
from structlog.testing import capture_logs

from va_api.auth.mfa_guard import (
    INVALID_CODE_MESSAGE,
    MFA_FAILURE_LIMIT,
    MFA_FAILURE_WINDOW_S,
    MFA_REPLAY_TTL_S,
    MfaGuard,
    invalid_code,
    mfa_failure_key,
    mfa_last_key,
    mfa_locked_message,
    mfa_secret_aad,
)
from va_common.errors import AppError
from va_common.ids import new_id


@pytest.fixture
def redis() -> Redis:
    return fakeredis.FakeAsyncRedis()


def _down() -> Redis:
    server = fakeredis.FakeServer()
    server.connected = False
    return fakeredis.FakeAsyncRedis(server=server)


def test_constants_keys_and_messages() -> None:
    user_id = new_id()
    assert (MFA_FAILURE_LIMIT, MFA_FAILURE_WINDOW_S, MFA_REPLAY_TTL_S) == (10, 900, 90)
    assert mfa_failure_key(user_id) == f"auth:fail:mfa:{user_id}"
    assert mfa_last_key(user_id) == f"mfa:last:{user_id}"
    assert mfa_secret_aad(user_id) == f"users.mfa_totp_secret_enc:{user_id}"
    assert mfa_locked_message(1) == "Too many invalid codes; try again in 1 second."
    error = invalid_code("recovery_code")
    assert (error.code, error.http_status, error.message, error.field) == (
        "INVALID_MFA_CODE",
        401,
        INVALID_CODE_MESSAGE,
        "recovery_code",
    )


async def test_claim_step_accepts_only_newer_steps(redis: Redis) -> None:
    guard = MfaGuard(redis)
    user_id, other = new_id(), new_id()
    assert await guard.claim_step(user_id, 1000) is True
    assert await guard.claim_step(user_id, 1000) is False
    assert await guard.claim_step(user_id, 999) is False
    assert await guard.claim_step(user_id, 1001) is True
    assert await guard.claim_step(other, 1000) is True
    assert int(await redis.get(mfa_last_key(user_id)) or 0) == 1001
    assert 0 < await redis.ttl(mfa_last_key(user_id)) <= MFA_REPLAY_TTL_S


async def test_forget_steps_allows_the_step_again(redis: Redis) -> None:
    guard = MfaGuard(redis)
    user_id = new_id()
    assert await guard.claim_step(user_id, 7) is True
    await guard.forget_steps(user_id)
    assert await redis.exists(mfa_last_key(user_id)) == 0
    assert await guard.claim_step(user_id, 7) is True


async def test_failures_lock_after_ten_and_clear_resets(redis: Redis) -> None:
    guard = MfaGuard(redis)
    user_id = new_id()
    for _ in range(MFA_FAILURE_LIMIT):
        await guard.check(user_id)
        await guard.record_failure(user_id)
    with pytest.raises(AppError) as info:
        await guard.check(user_id)
    assert (info.value.code, info.value.http_status) == ("AUTH_LOCKED", 429)
    assert info.value.message == "Too many invalid codes; try again in 900 seconds."
    assert info.value.headers == {"Retry-After": "900"}
    await guard.clear(user_id)
    await guard.check(user_id)


async def test_redis_down_allows_and_logs() -> None:
    guard = MfaGuard(_down())
    user_id = new_id()
    with capture_logs() as logs:
        assert await guard.claim_step(user_id, 5) is True
        assert await guard.claim_step(user_id, 5) is True
        for _ in range(MFA_FAILURE_LIMIT + 1):
            await guard.record_failure(user_id)
        await guard.check(user_id)
        await guard.clear(user_id)
        await guard.forget_steps(user_id)
    assert {entry["event"] for entry in logs} == {"mfa_guard_unavailable"}
