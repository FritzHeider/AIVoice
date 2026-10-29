"""Fixed-window failure counters that lock a key at a limit (specs/04-auth-tenancy-rbac.md §4.2, §4.5).

Each counter is a Redis key incremented on a failure, created with a window TTL (``EXPIRE NX``). The failure
that reaches the key's limit restarts the window, so the lock lasts a full window from it; locked attempts are
not counted. :meth:`FailureCounters.check` raises ``AUTH_LOCKED`` with ``Retry-After`` (the key's TTL). Redis
being unavailable logs ``unavailable_event`` and never blocks the caller. The login throttle and the MFA guard
share these mechanics.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from redis.asyncio import Redis
from redis.exceptions import RedisError

from va_common.errors import AppError
from va_common.logging import get_logger

__all__ = ["FailureCounters"]

_log = get_logger(__name__)


class FailureCounters:
    """Counters of ``(key, limit)`` pairs over one window length."""

    def __init__(
        self, redis: Redis, *, window_s: int, unavailable_event: str, locked_message: Callable[[int], str]
    ) -> None:
        self._redis = redis
        self._window_s = window_s
        self._unavailable_event = unavailable_event
        self._locked_message = locked_message

    def _unavailable(self, exc: BaseException) -> None:
        _log.warning(self._unavailable_event, error=type(exc).__name__)

    async def check(self, counters: Sequence[tuple[str, int]]) -> None:
        """``AUTH_LOCKED`` (with ``Retry-After``) when any key has reached its limit."""
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                for key, _ in counters:
                    pipe.get(key)
                    pipe.ttl(key)
                replies = await pipe.execute()
        except (RedisError, OSError) as exc:
            self._unavailable(exc)
            return
        for index, (_, limit) in enumerate(counters):
            count, ttl = replies[2 * index], int(replies[2 * index + 1])
            if count is not None and int(count) >= limit:
                retry_after = ttl if ttl > 0 else self._window_s
                raise AppError(
                    "AUTH_LOCKED",
                    self._locked_message(retry_after),
                    headers={"Retry-After": str(retry_after)},
                )

    async def record(self, counters: Sequence[tuple[str, int]]) -> None:
        """Count one failure on every key; the failure that reaches a limit restarts that key's window."""
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                for key, _ in counters:
                    pipe.incr(key)
                    pipe.expire(key, self._window_s, nx=True)
                replies = await pipe.execute()
            for index, (key, limit) in enumerate(counters):
                if int(replies[2 * index]) == limit:
                    await self._redis.expire(key, self._window_s)
        except (RedisError, OSError) as exc:
            self._unavailable(exc)

    async def clear(self, key: str) -> None:
        """Forget ``key``'s failures."""
        try:
            await self._redis.delete(key)
        except (RedisError, OSError) as exc:
            self._unavailable(exc)
