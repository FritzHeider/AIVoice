"""Atomic Redis token bucket (specs/24-billing-and-limits.md §6 item 1, specs/04-auth-tenancy-rbac.md §11).

One Lua script refills and takes in a single round-trip, so concurrent callers can never take more than the
bucket holds. State is a hash with fields ``tokens`` (float, as a string) and ``ts`` (last refill, epoch ms);
the key expires after twice the time an empty bucket needs to fill (an expired bucket is a full one). The
caller supplies ``now_ms`` from its injected clock, which keeps tests deterministic; a clock that goes
backwards never mints tokens. Redis errors propagate: the caller decides whether to fail open.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

from redis.asyncio import Redis

__all__ = ["TOKEN_BUCKET_LUA", "BucketDecision", "BucketLimit", "TokenBucket"]

# KEYS[1] bucket key; ARGV rate (tokens/s), burst (capacity), now_ms, cost.
# Returns {allowed (0|1), floor(tokens_left * 1000), retry_after_ms}: Redis truncates Lua numbers to integers,
# so the fractional token count travels as thousandths.
TOKEN_BUCKET_LUA: Final = """
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil or ts == nil then
  tokens = burst
  ts = now
end
if now > ts then
  tokens = math.min(burst, tokens + (now - ts) * rate / 1000)
  ts = now
end
local allowed = 0
local retry_ms = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  retry_ms = math.ceil((cost - tokens) * 1000 / rate)
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', string.format('%d', ts))
redis.call('PEXPIRE', KEYS[1], math.ceil(2 * burst * 1000 / rate))
return {allowed, math.floor(tokens * 1000), retry_ms}
"""  # noqa: S105 - a Lua script, not a credential (the name contains "TOKEN")

_MILLI: Final = 1000


@dataclass(frozen=True, slots=True)
class BucketLimit:
    """Refill rate in tokens per second and capacity (burst) in tokens."""

    rate_per_s: float
    burst: int

    def __post_init__(self) -> None:
        if not self.rate_per_s > 0:
            msg = f"rate_per_s must be positive: {self.rate_per_s}"
            raise ValueError(msg)
        if self.burst <= 0:
            msg = f"burst must be positive: {self.burst}"
            raise ValueError(msg)

    @property
    def fill_time_s(self) -> float:
        """Seconds an empty bucket needs to become full."""
        return self.burst / self.rate_per_s


@dataclass(frozen=True, slots=True)
class BucketDecision:
    """Outcome of one take: ``limit`` = capacity, ``remaining`` = whole tokens left, times in whole seconds.

    ``reset_s`` is the delay until the bucket is full again; ``retry_after_s`` is 0 when allowed, else the
    delay (at least 1 s) until enough tokens have accumulated.
    """

    allowed: bool
    limit: int
    remaining: int
    reset_s: int
    retry_after_s: int


class TokenBucket:
    """Token buckets stored in Redis; the script is sent once and then called by SHA (``EVALSHA``)."""

    def __init__(self, redis: Redis) -> None:
        self._script = redis.register_script(TOKEN_BUCKET_LUA)

    async def take(self, key: str, limit: BucketLimit, *, now_ms: int, cost: int = 1) -> BucketDecision:
        """Refill ``key`` up to ``now_ms`` and take ``cost`` tokens if available (else take nothing)."""
        if cost < 1:
            msg = f"cost must be at least 1: {cost}"
            raise ValueError(msg)
        allowed, tokens_milli, retry_ms = await self._script(
            keys=[key], args=[repr(float(limit.rate_per_s)), limit.burst, now_ms, cost]
        )
        missing_milli = max(0, limit.burst * _MILLI - int(tokens_milli))
        return BucketDecision(
            allowed=bool(allowed),
            limit=limit.burst,
            remaining=max(0, int(tokens_milli) // _MILLI),
            reset_s=math.ceil(missing_milli / (limit.rate_per_s * _MILLI)),
            retry_after_s=0 if allowed else max(1, math.ceil(int(retry_ms) / _MILLI)),
        )
