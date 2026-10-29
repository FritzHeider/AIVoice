"""Fixed-window attempt counters in Redis (the verification resend limit, the agent onboarding limits).

One ``INCR`` plus ``EXPIRE … NX`` per attempt: the first attempt opens a window of ``window_s`` seconds and
every attempt in it counts, rejected ones included. Beyond ``limit`` the attempt is ``RATE_LIMITED`` with
``Retry-After`` set to the seconds left in the window. When Redis is unavailable a fail-open counter (one
given an ``unavailable_event``) logs that event and lets the attempt through; a fail-closed one answers
``SERVICE_UNAVAILABLE``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from redis.asyncio import Redis
from redis.exceptions import RedisError

from va_common.errors import AppError
from va_common.logging import get_logger

__all__ = ["COUNTER_UNAVAILABLE_MESSAGE", "hit_window"]

COUNTER_UNAVAILABLE_MESSAGE: Final = "Rate limiting is temporarily unavailable; try again shortly."

_log = get_logger(__name__)


async def hit_window(  # noqa: PLR0913 - the counter, its window, and its failure behaviour
    redis: Redis,
    key: str,
    *,
    limit: int,
    window_s: int,
    message: Callable[[int], str],
    unavailable_event: str | None,
) -> None:
    """Count one attempt at ``key``; ``RATE_LIMITED`` with ``message(retry_after)`` beyond ``limit``."""
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, window_s, nx=True)
            pipe.ttl(key)
            count, _, ttl = await pipe.execute()
    except (RedisError, OSError) as exc:
        if unavailable_event is None:
            raise AppError("SERVICE_UNAVAILABLE", COUNTER_UNAVAILABLE_MESSAGE) from exc
        _log.warning(unavailable_event, error=type(exc).__name__)
        return
    if int(count) > limit:
        retry_after = int(ttl) if int(ttl) > 0 else window_s
        raise AppError("RATE_LIMITED", message(retry_after), headers={"Retry-After": str(retry_after)})
