"""Client-side rate limiting: per-account, per-category token buckets that wait up to 2 s
(specs/06-telephony-telnyx.md §2.2, Decision 2026-11-01 P03-T08a)."""

from __future__ import annotations

import fakeredis
import httpx
import pytest
import respx
from service_helpers import json_log_stream, log_events
from telnyx_client_helpers import API_KEY, BASE_URL, CCID, RecordingClock, rig

from va_common.token_bucket import BucketLimit, TokenBucket
from va_telnyx import TelnyxClient, TelnyxRateLimited, TelnyxRateLimiter
from va_telnyx.rate_limit import LIMITS, MAX_WAIT_S, RateCategory, bucket_key

OK = {"data": {"result": "ok"}}


def test_spec_limits() -> None:
    assert dict(LIMITS) == {
        "call_command": BucketLimit(50, 100),
        "number_search": BucketLimit(5, 5),
        "number_order": BucketLimit(2, 2),
        "messaging": BucketLimit(20, 20),
    }
    assert MAX_WAIT_S == 2.0
    assert bucket_key("byo-1", "messaging") == "rl:telnyx:byo-1:messaging"


async def test_burst_of_100_commands_passes_then_the_101st_waits_20_ms(telnyx_api: respx.MockRouter) -> None:
    route = telnyx_api.post(f"/calls/{CCID}/actions/hangup").respond(200, json=OK)
    async with httpx.AsyncClient() as http:
        harness = rig(http)
        for _ in range(100):
            await harness.client.hangup(CCID, command_id="h")
        assert harness.limiter_clock.sleeps == []
        await harness.client.hangup(CCID, command_id="h")
    assert harness.limiter_clock.sleeps == [0.02]
    assert route.call_count == 101
    assert await harness.redis.exists("rl:telnyx:platform:call_command")


async def test_number_search_waits_for_its_sixth_token() -> None:
    clock = RecordingClock()
    limiter = TelnyxRateLimiter(fakeredis.FakeAsyncRedis(), clock=clock)
    for _ in range(5):
        await limiter.acquire("platform", "number_search")
    await limiter.acquire("platform", "number_search")
    assert clock.sleeps == [0.2]


class ContendedClock(RecordingClock):
    """Another process takes the token whenever the limiter sleeps for it."""

    def __init__(self, redis: fakeredis.FakeAsyncRedis, limit: BucketLimit) -> None:
        super().__init__()
        self._thief = TokenBucket(redis)
        self._limit = limit

    async def sleep(self, seconds: float) -> None:
        await super().sleep(seconds)
        now_ms = int(self.now().timestamp() * 1000)
        await self._thief.take(bucket_key("platform", "number_order"), self._limit, now_ms=now_ms)


async def test_waits_within_one_acquire_add_up_to_the_budget_then_raise() -> None:
    redis = fakeredis.FakeAsyncRedis()
    limit = BucketLimit(1, 1)
    clock = ContendedClock(redis, limit)
    limiter = TelnyxRateLimiter(redis, clock=clock, limits={**LIMITS, "number_order": limit})
    await limiter.acquire("platform", "number_order")
    with pytest.raises(TelnyxRateLimited) as caught:
        await limiter.acquire("platform", "number_order", operation="create_number_order")
    assert clock.sleeps == [1.0, 1.0]
    assert (caught.value.category, caught.value.operation) == ("number_order", "create_number_order")
    assert caught.value.retry_after_s == 1.0


async def test_a_wait_longer_than_two_seconds_raises_at_once() -> None:
    clock = RecordingClock()
    limiter = TelnyxRateLimiter(
        fakeredis.FakeAsyncRedis(), clock=clock, limits={**LIMITS, "messaging": BucketLimit(0.25, 1)}
    )
    await limiter.acquire("platform", "messaging")
    with pytest.raises(TelnyxRateLimited):
        await limiter.acquire("platform", "messaging")
    assert clock.sleeps == []


async def test_rate_limited_client_makes_no_request(telnyx_api: respx.MockRouter) -> None:
    route = telnyx_api.post(f"/calls/{CCID}/actions/hangup").respond(200, json=OK)
    limiter = TelnyxRateLimiter(fakeredis.FakeAsyncRedis(), clock=RecordingClock(), max_wait_s=0.0)
    async with httpx.AsyncClient() as http:
        client = TelnyxClient(API_KEY, BASE_URL, http, limiter, account="byo-7", clock=RecordingClock())
        for _ in range(100):
            await client.hangup(CCID, command_id="h")
        with pytest.raises(TelnyxRateLimited) as caught:
            await client.hangup(CCID, command_id="h")
    assert route.call_count == 100
    assert (caught.value.account, caught.value.category, caught.value.operation) == (
        "byo-7",
        "call_command",
        "hangup",
    )
    assert caught.value.to_app_error().code == "SERVICE_UNAVAILABLE"


@pytest.mark.parametrize(("account", "category"), [("byo-1", "number_search"), ("platform", "number_order")])
async def test_buckets_are_per_account_and_category(account: str, category: RateCategory) -> None:
    clock = RecordingClock()
    limiter = TelnyxRateLimiter(fakeredis.FakeAsyncRedis(), clock=clock)
    for _ in range(5):
        await limiter.acquire("platform", "number_search")
    await limiter.acquire(account, category)
    assert clock.sleeps == []


async def test_redis_failure_fails_open(reset_logging: None) -> None:
    stream = json_log_stream()
    server = fakeredis.FakeServer()
    server.connected = False
    limiter = TelnyxRateLimiter(fakeredis.FakeAsyncRedis(server=server), clock=RecordingClock())
    await limiter.acquire("platform", "call_command")
    [event] = [e for e in log_events(stream) if e["event"] == "telnyx_rate_limiter_unavailable"]
    assert (event["account"], event["category"], event["error"]) == (
        "platform",
        "call_command",
        "ConnectionError",
    )
    assert event["level"] == "warning"
