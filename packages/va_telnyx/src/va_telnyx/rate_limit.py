"""Client-side Telnyx rate limiting (specs/06-telephony-telnyx.md §2.2, Decision 2026-11-01 P03-T08a).

One Redis token bucket per Telnyx account and category, ``rl:telnyx:{account}:{category}``: call commands 50/s
(burst 100), number search 5/s, number orders 2/s, messaging 20/s (burst = rate where the spec names none).
:meth:`TelnyxRateLimiter.acquire` waits on the injected clock for at most 2 s, then raises
:class:`TelnyxRateLimited`. A Redis failure fails open (logged): Telnyx's own ``429`` plus the retry policy
still protect the account.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal

from redis.asyncio import Redis
from redis.exceptions import RedisError

from va_common.logging import get_logger
from va_common.time import Clock, SystemClock
from va_common.token_bucket import BucketLimit, TokenBucket
from va_telnyx.errors import TelnyxRateLimited

__all__ = ["LIMITS", "MAX_WAIT_S", "RateCategory", "TelnyxRateLimiter", "bucket_key"]

type RateCategory = Literal["call_command", "number_search", "number_order", "messaging"]

LIMITS: Final[Mapping[RateCategory, BucketLimit]] = MappingProxyType(
    {
        "call_command": BucketLimit(rate_per_s=50, burst=100),
        "number_search": BucketLimit(rate_per_s=5, burst=5),
        "number_order": BucketLimit(rate_per_s=2, burst=2),
        "messaging": BucketLimit(rate_per_s=20, burst=20),
    }
)
MAX_WAIT_S: Final = 2.0
_MS: Final = 1000

_log = get_logger(__name__)


def bucket_key(account: str, category: RateCategory) -> str:
    """Redis key of one account's bucket for ``category``."""
    return f"rl:telnyx:{account}:{category}"


class TelnyxRateLimiter:
    """Token buckets shared by every process that talks to Telnyx with the same account."""

    def __init__(
        self,
        redis: Redis,
        *,
        clock: Clock | None = None,
        max_wait_s: float = MAX_WAIT_S,
        limits: Mapping[RateCategory, BucketLimit] = LIMITS,
    ) -> None:
        self._bucket = TokenBucket(redis)
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._max_wait_s = max_wait_s
        self._limits = limits

    async def acquire(self, account: str, category: RateCategory, *, operation: str = "") -> None:
        """Take one token, sleeping until one is available; raise :class:`TelnyxRateLimited` when the next
        token would arrive after the wait budget."""
        key = bucket_key(account, category)
        limit = self._limits[category]
        waited = 0.0
        while True:
            try:
                decision = await self._bucket.take(
                    key, limit, now_ms=int(self._clock.now().timestamp() * _MS)
                )
            except RedisError as exc:
                _log.warning(
                    "telnyx_rate_limiter_unavailable",
                    account=account,
                    category=category,
                    error=type(exc).__name__,
                )
                return
            if decision.allowed:
                return
            delay = decision.retry_after_ms / _MS
            if waited + delay > self._max_wait_s:
                raise TelnyxRateLimited(
                    account=account, category=category, retry_after_s=delay, operation=operation
                )
            await self._clock.sleep(delay)
            waited += delay
