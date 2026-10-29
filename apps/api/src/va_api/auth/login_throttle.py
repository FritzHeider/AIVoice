"""Password-login throttling (specs/04-auth-tenancy-rbac.md §4.2).

Failed logins are counted per email (``auth:fail:email:<sha256 of the casefolded email>``) and per client IP
(``auth:fail:ip:<ip>``) in 15-minute windows. 10 failures for an email or 50 for an IP lock further attempts
for 15 minutes from the locking failure with ``AUTH_LOCKED`` and ``Retry-After``; locked attempts are not
counted. A successful login clears the email counter only. Redis being unavailable never blocks a login.
"""

from __future__ import annotations

from typing import Final

from redis.asyncio import Redis
from redis.exceptions import RedisError

from va_common.auth import sha256_hex
from va_common.errors import AppError
from va_common.logging import get_logger

__all__ = [
    "EMAIL_FAILURE_LIMIT",
    "FAILURE_WINDOW_S",
    "IP_FAILURE_LIMIT",
    "LoginThrottle",
    "email_failure_key",
    "ip_failure_key",
    "locked_message",
    "seconds_phrase",
]

EMAIL_FAILURE_LIMIT: Final = 10
IP_FAILURE_LIMIT: Final = 50
FAILURE_WINDOW_S: Final = 900

_log = get_logger(__name__)


def email_failure_key(email: str) -> str:
    return f"auth:fail:email:{sha256_hex(email.strip().casefold())}"


def ip_failure_key(ip: str) -> str:
    return f"auth:fail:ip:{ip}"


def seconds_phrase(seconds: int) -> str:
    """``"1 second"`` or ``"<n> seconds"``."""
    return f"{seconds} {'second' if seconds == 1 else 'seconds'}"


def locked_message(retry_after_s: int) -> str:
    return f"Too many failed login attempts; try again in {seconds_phrase(retry_after_s)}."


def _counters(email: str, ip: str | None) -> list[tuple[str, int]]:
    counters = [(email_failure_key(email), EMAIL_FAILURE_LIMIT)]
    if ip is not None:
        counters.append((ip_failure_key(ip), IP_FAILURE_LIMIT))
    return counters


class LoginThrottle:
    """Failure counters for ``POST /internal/auth/login``."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def check(self, email: str, ip: str | None) -> None:
        """``AUTH_LOCKED`` (with ``Retry-After``) when the email or the IP is locked."""
        counters = _counters(email, ip)
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                for key, _ in counters:
                    pipe.get(key)
                    pipe.ttl(key)
                replies = await pipe.execute()
        except (RedisError, OSError) as exc:
            _log.warning("login_throttle_unavailable", error=type(exc).__name__)
            return
        for index, (_, limit) in enumerate(counters):
            count, ttl = replies[2 * index], int(replies[2 * index + 1])
            if count is not None and int(count) >= limit:
                retry_after = ttl if ttl > 0 else FAILURE_WINDOW_S
                raise AppError(
                    "AUTH_LOCKED", locked_message(retry_after), headers={"Retry-After": str(retry_after)}
                )

    async def record_failure(self, email: str, ip: str | None) -> None:
        """Count one failed attempt; the failure that reaches a limit restarts that key's 15-minute window."""
        counters = _counters(email, ip)
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                for key, _ in counters:
                    pipe.incr(key)
                    pipe.expire(key, FAILURE_WINDOW_S, nx=True)
                replies = await pipe.execute()
            for index, (key, limit) in enumerate(counters):
                if int(replies[2 * index]) == limit:
                    await self._redis.expire(key, FAILURE_WINDOW_S)
        except (RedisError, OSError) as exc:
            _log.warning("login_throttle_unavailable", error=type(exc).__name__)

    async def clear(self, email: str) -> None:
        """Forget the email's failures after a successful login."""
        try:
            await self._redis.delete(email_failure_key(email))
        except (RedisError, OSError) as exc:
            _log.warning("login_throttle_unavailable", error=type(exc).__name__)
