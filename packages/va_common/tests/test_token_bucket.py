"""The Redis Lua token bucket against fakeredis with Lua (specs/24-billing-and-limits.md §6 item 1)."""

from __future__ import annotations

import asyncio

import fakeredis
import pytest

from va_common.token_bucket import BucketDecision, BucketLimit, TokenBucket

KEY = "rl:key:{0193c5d2}:default"
START_MS = 1_767_225_600_000
TEN_PER_S = BucketLimit(rate_per_s=10.0, burst=20)


@pytest.fixture
def redis() -> fakeredis.FakeAsyncRedis:
    return fakeredis.FakeAsyncRedis()


async def _drain(bucket: TokenBucket, limit: BucketLimit, now_ms: int) -> None:
    for _ in range(limit.burst):
        assert (await bucket.take(KEY, limit, now_ms=now_ms)).allowed


async def test_fresh_bucket_starts_full(redis: fakeredis.FakeAsyncRedis) -> None:
    decision = await TokenBucket(redis).take(KEY, TEN_PER_S, now_ms=START_MS)
    assert decision == BucketDecision(allowed=True, limit=20, remaining=19, reset_s=1, retry_after_s=0)


async def test_exhaustion_denies_with_retry_after(redis: fakeredis.FakeAsyncRedis) -> None:
    bucket = TokenBucket(redis)
    await _drain(bucket, TEN_PER_S, START_MS)
    denied = await bucket.take(KEY, TEN_PER_S, now_ms=START_MS)
    assert denied == BucketDecision(allowed=False, limit=20, remaining=0, reset_s=2, retry_after_s=1)


@pytest.mark.parametrize(
    ("elapsed_ms", "available"),
    [(99, 0), (100, 1), (550, 5), (1_000, 10), (10_000, 20)],
)
async def test_refill_math(redis: fakeredis.FakeAsyncRedis, elapsed_ms: int, available: int) -> None:
    bucket = TokenBucket(redis)
    await _drain(bucket, TEN_PER_S, START_MS)
    now = START_MS + elapsed_ms
    allowed = 0
    while (await bucket.take(KEY, TEN_PER_S, now_ms=now)).allowed:
        allowed += 1
    assert allowed == available


async def test_refill_is_capped_at_burst(redis: fakeredis.FakeAsyncRedis) -> None:
    bucket = TokenBucket(redis)
    await _drain(bucket, TEN_PER_S, START_MS)
    decision = await bucket.take(KEY, TEN_PER_S, now_ms=START_MS + 60_000)
    assert (decision.remaining, decision.reset_s) == (19, 1)


async def test_fractional_rate(redis: fakeredis.FakeAsyncRedis) -> None:
    bucket, limit = TokenBucket(redis), BucketLimit(rate_per_s=0.5, burst=1)
    assert (await bucket.take(KEY, limit, now_ms=START_MS)).reset_s == 2
    denied = await bucket.take(KEY, limit, now_ms=START_MS + 500)
    assert (denied.allowed, denied.retry_after_s) == (False, 2)
    assert (await bucket.take(KEY, limit, now_ms=START_MS + 2_000)).allowed


async def test_cost_greater_than_one(redis: fakeredis.FakeAsyncRedis) -> None:
    bucket, limit = TokenBucket(redis), BucketLimit(rate_per_s=1.0, burst=5)
    first = await bucket.take(KEY, limit, now_ms=START_MS, cost=3)
    assert (first.allowed, first.remaining, first.reset_s) == (True, 2, 3)
    denied = await bucket.take(KEY, limit, now_ms=START_MS, cost=3)
    assert (denied.allowed, denied.remaining, denied.retry_after_s) == (False, 2, 1)
    assert (await bucket.take(KEY, limit, now_ms=START_MS, cost=2)).allowed


async def test_cost_above_burst_never_allowed(redis: fakeredis.FakeAsyncRedis) -> None:
    bucket, limit = TokenBucket(redis), BucketLimit(rate_per_s=2.0, burst=4)
    for later_ms in (0, 60_000):
        decision = await bucket.take(KEY, limit, now_ms=START_MS + later_ms, cost=6)
        assert not decision.allowed
        assert decision.remaining == 4
        assert decision.retry_after_s == 1


async def test_clock_going_backwards_does_not_mint_tokens(redis: fakeredis.FakeAsyncRedis) -> None:
    bucket = TokenBucket(redis)
    await _drain(bucket, TEN_PER_S, START_MS)
    assert not (await bucket.take(KEY, TEN_PER_S, now_ms=START_MS - 5_000)).allowed
    # The stored timestamp did not move backwards: 100 ms after the original time refills exactly one token.
    assert (await bucket.take(KEY, TEN_PER_S, now_ms=START_MS + 100)).allowed
    assert not (await bucket.take(KEY, TEN_PER_S, now_ms=START_MS + 100)).allowed


async def test_state_and_ttl(redis: fakeredis.FakeAsyncRedis) -> None:
    await TokenBucket(redis).take(KEY, TEN_PER_S, now_ms=START_MS)
    assert await redis.hgetall(KEY) == {b"tokens": b"19", b"ts": str(START_MS).encode()}
    # Twice the fill time: 2 x 20 tokens / 10 per second = 4 s.
    assert 3_900 <= await redis.pttl(KEY) <= 4_000


async def test_buckets_are_independent_per_key(redis: fakeredis.FakeAsyncRedis) -> None:
    bucket = TokenBucket(redis)
    await _drain(bucket, TEN_PER_S, START_MS)
    assert (await bucket.take("rl:key:{other}:default", TEN_PER_S, now_ms=START_MS)).remaining == 19


@pytest.mark.parametrize(("rate", "burst"), [(0.0, 1), (-1.0, 1), (1.0, 0), (1.0, -5)])
def test_bucket_limit_validation(rate: float, burst: int) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        BucketLimit(rate_per_s=rate, burst=burst)


def test_fill_time() -> None:
    assert TEN_PER_S.fill_time_s == 2.0
    assert BucketLimit(rate_per_s=0.5, burst=3).fill_time_s == 6.0


@pytest.mark.parametrize("cost", [0, -1])
async def test_cost_must_be_positive(redis: fakeredis.FakeAsyncRedis, cost: int) -> None:
    with pytest.raises(ValueError, match="cost must be at least 1"):
        await TokenBucket(redis).take(KEY, TEN_PER_S, now_ms=START_MS, cost=cost)
    assert await redis.exists(KEY) == 0


async def test_concurrent_takes_never_exceed_burst(redis: fakeredis.FakeAsyncRedis) -> None:
    bucket = TokenBucket(redis)
    decisions = await asyncio.gather(*(bucket.take(KEY, TEN_PER_S, now_ms=START_MS) for _ in range(100)))
    assert sum(decision.allowed for decision in decisions) == 20
    assert sorted(decision.remaining for decision in decisions if decision.allowed) == list(range(20))
