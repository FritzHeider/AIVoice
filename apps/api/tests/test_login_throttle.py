"""Password-login throttling on fake Redis (specs/04-auth-tenancy-rbac.md §4.2)."""

from __future__ import annotations

import fakeredis
import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from va_api.auth.login_throttle import (
    EMAIL_FAILURE_LIMIT,
    FAILURE_WINDOW_S,
    IP_FAILURE_LIMIT,
    LoginThrottle,
    email_failure_key,
    ip_failure_key,
    locked_message,
)
from va_common.auth import sha256_hex
from va_common.errors import AppError

EMAIL = "Jane@Acme-Dental.test"
IP = "203.0.113.9"


@pytest.fixture
def redis() -> Redis:
    return fakeredis.FakeAsyncRedis()


async def _locked(throttle: LoginThrottle, email: str, ip: str | None) -> AppError:
    with pytest.raises(AppError) as info:
        await throttle.check(email, ip)
    return info.value


def test_limits_and_keys() -> None:
    assert (EMAIL_FAILURE_LIMIT, IP_FAILURE_LIMIT, FAILURE_WINDOW_S) == (10, 50, 900)
    assert (
        email_failure_key(" jane@acme-dental.TEST ")
        == f"auth:fail:email:{sha256_hex('jane@acme-dental.test')}"
    )
    assert ip_failure_key(IP) == "auth:fail:ip:203.0.113.9"
    assert locked_message(1) == "Too many failed login attempts; try again in 1 second."
    assert locked_message(900) == "Too many failed login attempts; try again in 900 seconds."


async def test_email_locks_at_ten_failures(redis: Redis) -> None:
    throttle = LoginThrottle(redis)
    for _ in range(EMAIL_FAILURE_LIMIT - 1):
        await throttle.check(EMAIL, IP)
        await throttle.record_failure(EMAIL, IP)
    await throttle.check(EMAIL, IP)
    await throttle.record_failure(EMAIL, IP)
    error = await _locked(throttle, EMAIL.lower(), None)
    ttl = await redis.ttl(email_failure_key(EMAIL))
    assert (error.code, error.http_status) == ("AUTH_LOCKED", 429)
    assert 0 < ttl <= FAILURE_WINDOW_S
    assert error.headers == {"Retry-After": str(ttl)}
    assert error.message == locked_message(ttl)
    # Locked attempts are not counted, and another email from the same IP is still allowed.
    await _locked(throttle, EMAIL, IP)
    assert int(await redis.get(email_failure_key(EMAIL)) or 0) == EMAIL_FAILURE_LIMIT
    await throttle.check("other@acme-dental.test", IP)
    await redis.delete(email_failure_key(EMAIL))
    await throttle.check(EMAIL, IP)


async def test_locking_failure_restarts_the_window(redis: Redis) -> None:
    throttle = LoginThrottle(redis)
    key = email_failure_key(EMAIL)
    for _ in range(EMAIL_FAILURE_LIMIT - 1):
        await throttle.record_failure(EMAIL, None)
    await redis.expire(key, 60)
    await throttle.record_failure(EMAIL, None)
    assert await redis.ttl(key) > 60


async def test_ip_locks_at_fifty_failures_across_emails(redis: Redis) -> None:
    throttle = LoginThrottle(redis)
    for index in range(IP_FAILURE_LIMIT):
        await throttle.record_failure(f"user{index}@acme-dental.test", IP)
    error = await _locked(throttle, "fresh@acme-dental.test", IP)
    assert error.code == "AUTH_LOCKED"
    await throttle.check("fresh@acme-dental.test", "198.51.100.1")
    await throttle.check("fresh@acme-dental.test", None)


async def test_clear_resets_email_only(redis: Redis) -> None:
    throttle = LoginThrottle(redis)
    await throttle.record_failure(EMAIL, IP)
    await throttle.clear(EMAIL)
    assert await redis.get(email_failure_key(EMAIL)) is None
    assert int(await redis.get(ip_failure_key(IP)) or 0) == 1


async def test_missing_ttl_falls_back_to_window(redis: Redis) -> None:
    await redis.set(email_failure_key(EMAIL), EMAIL_FAILURE_LIMIT)
    error = await _locked(LoginThrottle(redis), EMAIL, None)
    assert error.headers == {"Retry-After": str(FAILURE_WINDOW_S)}


async def test_redis_failures_fail_open() -> None:
    server = fakeredis.FakeServer()
    server.connected = False
    down = fakeredis.FakeAsyncRedis(server=server)
    with pytest.raises(RedisConnectionError):
        await down.get("probe")
    async with down.pipeline(transaction=True) as pipe:
        pipe.get("probe")
        with pytest.raises(RedisConnectionError):
            await pipe.execute()
    throttle = LoginThrottle(down)
    await throttle.check(EMAIL, IP)
    await throttle.record_failure(EMAIL, IP)
    await throttle.clear(EMAIL)
