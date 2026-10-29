"""MFA code brute-force lock and TOTP replay guard (specs/04-auth-tenancy-rbac.md §4.5; specs/03 §11).

Wrong MFA codes are counted per user at ``auth:fail:mfa:<user_id>``: 10 failures in 15 minutes lock MFA code
checks for 15 minutes from the locking failure with ``AUTH_LOCKED`` (the login throttle's mechanics, but a
separate key, because a successful password login clears the email counter). A success clears the counter.
An accepted TOTP time step is claimed at ``mfa:last:<user_id>`` (TTL 90 s) by a compare-and-set that accepts
only a step greater than the stored one, so a code is accepted at most once. Redis being unavailable logs
``mfa_guard_unavailable`` and allows the attempt (the ±1 step window bounds a replay to 90 s).
"""

from __future__ import annotations

import uuid
from typing import Final

from redis.asyncio import Redis
from redis.exceptions import RedisError

from va_api.auth.failure_counter import FailureCounters
from va_api.auth.login_throttle import seconds_phrase
from va_common.errors import AppError
from va_common.logging import get_logger

__all__ = [
    "ALREADY_ENABLED_MESSAGE",
    "INVALID_CODE_MESSAGE",
    "MFA_FAILURE_LIMIT",
    "MFA_FAILURE_WINDOW_S",
    "MFA_REPLAY_TTL_S",
    "NOT_ENABLED_MESSAGE",
    "ORG_REQUIRES_MFA_MESSAGE",
    "REPLAY_GUARD_LUA",
    "SETUP_CHANGED_MESSAGE",
    "SETUP_FIRST_MESSAGE",
    "UNAVAILABLE_EVENT",
    "MfaGuard",
    "invalid_code",
    "mfa_failure_key",
    "mfa_last_key",
    "mfa_locked_message",
    "mfa_secret_aad",
]

MFA_FAILURE_LIMIT: Final = 10
MFA_FAILURE_WINDOW_S: Final = 900
MFA_REPLAY_TTL_S: Final = 90
UNAVAILABLE_EVENT: Final = "mfa_guard_unavailable"
INVALID_CODE_MESSAGE: Final = "Invalid or expired code."
NOT_ENABLED_MESSAGE: Final = "Multi-factor authentication is not enabled."
ALREADY_ENABLED_MESSAGE: Final = "Multi-factor authentication is already enabled."
SETUP_FIRST_MESSAGE: Final = "Start multi-factor setup first."
SETUP_CHANGED_MESSAGE: Final = "Multi-factor setup changed; start again."
ORG_REQUIRES_MFA_MESSAGE: Final = "Multi-factor authentication is required by one of your organizations."

# KEYS[1] mfa:last:<user_id>; ARGV accepted step counter, TTL seconds. Returns 1 when the step is newer than
# the stored one (and is now stored), 0 for a replay (an equal or older step).
REPLAY_GUARD_LUA: Final = """
local stored = redis.call('GET', KEYS[1])
if stored and tonumber(stored) >= tonumber(ARGV[1]) then
  return 0
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return 1
"""

_log = get_logger(__name__)


def mfa_failure_key(user_id: uuid.UUID) -> str:
    return f"auth:fail:mfa:{user_id}"


def mfa_last_key(user_id: uuid.UUID) -> str:
    return f"mfa:last:{user_id}"


def mfa_secret_aad(user_id: uuid.UUID) -> str:
    """The AAD of ``users.mfa_totp_secret_enc`` (specs/28-security.md §3 (j))."""
    return f"users.mfa_totp_secret_enc:{user_id}"


def mfa_locked_message(retry_after_s: int) -> str:
    return f"Too many invalid codes; try again in {seconds_phrase(retry_after_s)}."


def invalid_code(field: str) -> AppError:
    """``INVALID_MFA_CODE`` 401 against ``field`` (``code`` or ``recovery_code``)."""
    return AppError("INVALID_MFA_CODE", INVALID_CODE_MESSAGE, field=field)


class MfaGuard:
    """The per-user MFA failure counter and the TOTP step replay guard."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._counters = FailureCounters(
            redis,
            window_s=MFA_FAILURE_WINDOW_S,
            unavailable_event=UNAVAILABLE_EVENT,
            locked_message=mfa_locked_message,
        )
        self._claim = redis.register_script(REPLAY_GUARD_LUA)

    async def check(self, user_id: uuid.UUID) -> None:
        """``AUTH_LOCKED`` (with ``Retry-After``) while the user's MFA code checks are locked."""
        await self._counters.check([(mfa_failure_key(user_id), MFA_FAILURE_LIMIT)])

    async def record_failure(self, user_id: uuid.UUID) -> None:
        """Count one wrong, malformed, or replayed code."""
        await self._counters.record([(mfa_failure_key(user_id), MFA_FAILURE_LIMIT)])

    async def clear(self, user_id: uuid.UUID) -> None:
        """Forget the user's failures after an accepted code."""
        await self._counters.clear(mfa_failure_key(user_id))

    async def claim_step(self, user_id: uuid.UUID, counter: int) -> bool:
        """Whether time step ``counter`` is newer than the last accepted one (it becomes the last one)."""
        try:
            claimed = await self._claim(keys=[mfa_last_key(user_id)], args=[counter, MFA_REPLAY_TTL_S])
        except (RedisError, OSError) as exc:
            _log.warning(UNAVAILABLE_EVENT, error=type(exc).__name__)
            return True
        return int(claimed) == 1

    async def forget_steps(self, user_id: uuid.UUID) -> None:
        """Drop the last accepted step (MFA was disabled)."""
        try:
            await self._redis.delete(mfa_last_key(user_id))
        except (RedisError, OSError) as exc:
            _log.warning(UNAVAILABLE_EVENT, error=type(exc).__name__)
