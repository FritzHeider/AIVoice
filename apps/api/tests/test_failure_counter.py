"""Fixed-window failure counters shared by the login throttle and the MFA guard (specs/04 §4.2, §4.5)."""

from __future__ import annotations

import fakeredis
import pytest
from redis.asyncio import Redis
from structlog.testing import capture_logs

from va_api.auth.failure_counter import FailureCounters
from va_common.errors import AppError

WINDOW_S = 600
KEY = "auth:fail:test:one"
OTHER = "auth:fail:test:two"


def _message(seconds: int) -> str:
    return f"Locked for {seconds}s."


def _counters(redis: Redis) -> FailureCounters:
    return FailureCounters(
        redis, window_s=WINDOW_S, unavailable_event="test_counter_down", locked_message=_message
    )


async def test_locks_at_limit_with_retry_after_from_ttl() -> None:
    redis = fakeredis.FakeAsyncRedis()
    counters = _counters(redis)
    pairs = [(KEY, 3), (OTHER, 5)]
    for _ in range(2):
        await counters.check(pairs)
        await counters.record(pairs)
    await counters.check(pairs)
    await counters.record(pairs)
    with pytest.raises(AppError) as info:
        await counters.check(pairs)
    ttl = await redis.ttl(KEY)
    assert 0 < ttl <= WINDOW_S
    assert (info.value.code, info.value.http_status) == ("AUTH_LOCKED", 429)
    assert info.value.message == _message(ttl)
    assert info.value.headers == {"Retry-After": str(ttl)}
    assert int(await redis.get(OTHER) or 0) == 3


async def test_locking_failure_restarts_the_window() -> None:
    redis = fakeredis.FakeAsyncRedis()
    counters = _counters(redis)
    await counters.record([(KEY, 2)])
    await redis.expire(KEY, 5)
    await counters.record([(KEY, 2)])
    assert await redis.ttl(KEY) == WINDOW_S


async def test_counter_without_ttl_reports_the_full_window() -> None:
    redis = fakeredis.FakeAsyncRedis()
    await redis.set(KEY, 3)
    with pytest.raises(AppError) as info:
        await _counters(redis).check([(KEY, 3)])
    assert info.value.headers == {"Retry-After": str(WINDOW_S)}


async def test_clear_forgets_failures() -> None:
    redis = fakeredis.FakeAsyncRedis()
    counters = _counters(redis)
    for _ in range(3):
        await counters.record([(KEY, 3)])
    await counters.clear(KEY)
    await counters.check([(KEY, 3)])
    assert await redis.exists(KEY) == 0


async def test_redis_down_allows_and_logs_the_configured_event() -> None:
    server = fakeredis.FakeServer()
    server.connected = False
    counters = _counters(fakeredis.FakeAsyncRedis(server=server))
    with capture_logs() as logs:
        await counters.record([(KEY, 1)])
        await counters.check([(KEY, 1)])
        await counters.clear(KEY)
    assert [entry["event"] for entry in logs] == ["test_counter_down"] * 3
    assert {entry["error"] for entry in logs} == {"ConnectionError"}
