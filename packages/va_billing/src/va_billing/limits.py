"""Send Call step 10: per-org dispatch limits in Redis (specs/24 §6, Decision P05-T04b; specs/28 §9.3).

Order (first failure wins): same-number 10-second rule, per-destination daily frequency, new-account
destination rule, daily cap, hourly cap, concurrency, international hourly cap. Each counter that was taken
is recorded in the returned :class:`LimitTicket`, and any later failure (inside this step or a later one such
as the credit hold) undoes them in reverse order with :meth:`LimitTicket.rollback`. The same-number key is
never undone: a rapid retry stays blocked even when the first attempt failed later.

Unlimited caps (``None``) still count, so the limits endpoint can report usage. Redis errors fail closed
(``SERVICE_UNAVAILABLE``): limits protect spend, unlike the request-rate bucket, which fails open.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from redis.asyncio import Redis
from redis.exceptions import RedisError

from va_billing import keys
from va_billing.plans import EffectiveLimits
from va_billing.scripts import LimitScripts
from va_common.errors import AppError
from va_common.logging import get_logger
from va_common.time import Clock
from va_compliance.calling_hours import destination_state_rule
from va_compliance.international import DOMESTIC_NANP
from va_domain.org import OrgSettings

__all__ = [
    "CONCURRENCY_CAP_MESSAGE",
    "DAILY_CAP_MESSAGE",
    "DESTINATION_FREQUENCY_MESSAGE",
    "HOURLY_CAP_MESSAGE",
    "INTERNATIONAL_RATE_MESSAGE",
    "LIMITS_UNAVAILABLE_MESSAGE",
    "NEW_ACCOUNT_CAPS_MESSAGE",
    "NEW_ACCOUNT_REGION_MESSAGE",
    "SAME_NUMBER_MESSAGE",
    "CallLimiter",
    "LimitInputs",
    "LimitTicket",
    "LimitUsage",
    "frequency_cap",
    "is_new_account",
]

SAME_NUMBER_MESSAGE: Final = "Please wait at least 10 seconds between calls to the same number."
DESTINATION_FREQUENCY_MESSAGE: Final = "This number has reached its daily call limit."
NEW_ACCOUNT_REGION_MESSAGE: Final = (
    "New accounts can call only US and Canadian numbers during their first 24 hours."
)
NEW_ACCOUNT_CAPS_MESSAGE: Final = (
    "New accounts are limited to 100 calls per day and 10 concurrent calls during their first 24 hours."
)
DAILY_CAP_MESSAGE: Final = "Daily call limit reached for this organization."
HOURLY_CAP_MESSAGE: Final = "Hourly call limit reached for this organization."
CONCURRENCY_CAP_MESSAGE: Final = "Concurrent call limit reached for this organization."
INTERNATIONAL_RATE_MESSAGE: Final = "International call limit per hour reached for this organization."
LIMITS_UNAVAILABLE_MESSAGE: Final = "Call limits are temporarily unavailable."

SAME_NUMBER_WINDOW_MS: Final = 10_000
DAY_TTL_S: Final = 172_800
HOUR_TTL_S: Final = 7_200
CONCURRENCY_GRACE_MIN: Final = 5
NEW_ACCOUNT_AGE: Final = timedelta(hours=24)
NEW_ACCOUNT_DAILY_CAP: Final = 100
NEW_ACCOUNT_CONCURRENCY_CAP: Final = 10
_MS_PER_MIN: Final = 60_000

_log = get_logger(__name__)

Undo = Callable[[], Awaitable[object]]


def is_new_account(created_at: datetime, now: datetime) -> bool:
    """Whether an org created at ``created_at`` is younger than 24 hours at ``now``."""
    return created_at > now - NEW_ACCOUNT_AGE


def frequency_cap(limits: EffectiveLimits, settings: OrgSettings, destination: str) -> int:
    """Calls per UTC day to one number: the platform/enterprise cap, the org's own setting, the state rule.

    The state statutes count 24 hours; the spec key counts the UTC calendar day (Decision P05-T04b).
    """
    caps = [limits.per_destination_daily_calls]
    if settings.compliance.max_attempts_per_number_per_day > 0:
        caps.append(settings.compliance.max_attempts_per_number_per_day)
    if settings.compliance.state_quiet_hours:
        rule = destination_state_rule(destination)
        if rule is not None and rule.max_calls_per_24h is not None:
            caps.append(rule.max_calls_per_24h)
    return min(caps)


def _tightened(cap: int | None, ceiling: int) -> int:
    return ceiling if cap is None else min(cap, ceiling)


def _shared(cap: int | None, share: float) -> int | None:
    return None if cap is None else max(1, math.floor(cap * share))


@dataclass(frozen=True, slots=True)
class LimitInputs:
    """One call's facts for the limit checks (``region`` = destination ISO region, ``None`` when unknown)."""

    org_id: uuid.UUID
    org_created_at: datetime
    limits: EffectiveLimits
    settings: OrgSettings
    destination: str
    region: str | None
    call_id: uuid.UUID
    max_duration_min: int
    concurrency_share: float = 1.0

    @property
    def international(self) -> bool:
        """Whether the destination is outside the domestic set (US, CA, the org's default country)."""
        return self.region not in DOMESTIC_NANP and self.region != self.settings.default_country


@dataclass(slots=True)
class LimitTicket:
    """The counters one admission took; :meth:`rollback` gives them back in reverse order (idempotent)."""

    _undo: list[tuple[str, Undo]] = field(default_factory=list)

    def add(self, label: str, undo: Undo) -> None:
        """Record how to give back the counter named ``label``."""
        self._undo.append((label, undo))

    @property
    def labels(self) -> tuple[str, ...]:
        """The counters held, in acquisition order."""
        return tuple(label for label, _ in self._undo)

    async def rollback(self) -> None:
        """Undo every recorded counter, newest first; a Redis error is logged and the rest still runs."""
        steps, self._undo = self._undo, []
        for label, undo in reversed(steps):
            try:
                await undo()
            except (RedisError, OSError) as exc:
                _log.warning("limits.rollback_failed", counter=label, error=type(exc).__name__)


@dataclass(frozen=True, slots=True)
class LimitUsage:
    """Current counter values of an org (for ``GET /v1/billing/limits``)."""

    daily: int
    hourly: int
    international: int
    concurrency: int


class CallLimiter:
    """The step-10 checks on one Redis client; times come from the injected clock."""

    def __init__(self, redis: Redis, *, clock: Clock) -> None:
        self._redis = redis
        self._scripts = LimitScripts(redis)
        self._clock = clock

    async def acquire(self, inputs: LimitInputs) -> LimitTicket:
        """Take every counter for one call or raise the first limit error (taken counters given back)."""
        ticket = LimitTicket()
        try:
            await self._take_all(inputs, self._clock.now(), ticket)
        except (RedisError, OSError) as exc:
            await ticket.rollback()
            raise AppError("SERVICE_UNAVAILABLE", LIMITS_UNAVAILABLE_MESSAGE) from exc
        except AppError:
            await ticket.rollback()
            raise
        return ticket

    async def _take_all(self, inputs: LimitInputs, now: datetime, ticket: LimitTicket) -> None:
        await self._same_number(inputs)
        await self._frequency(inputs, now, ticket)
        young = is_new_account(inputs.org_created_at, now)
        if young and inputs.region not in DOMESTIC_NANP:
            raise AppError("NEW_ACCOUNT_LIMIT", NEW_ACCOUNT_REGION_MESSAGE, field="phone_number")
        await self._daily(inputs, now, young, ticket)
        await self._counter(
            keys.hourly_key(inputs.org_id, now), inputs.limits.hourly_calls, HOUR_TTL_S, "hourly", ticket
        )
        await self._concurrency(inputs, now, young, ticket)
        if inputs.international:
            await self._counter(
                keys.international_key(inputs.org_id, now),
                inputs.limits.international_hourly_calls,
                HOUR_TTL_S,
                "international",
                ticket,
            )

    async def release_call(self, org_id: uuid.UUID, call_id: uuid.UUID) -> None:
        """Free the call's concurrency slot (idempotent; called when the call ends)."""
        await self._redis.zrem(keys.concurrency_key(org_id), str(call_id))

    async def active_calls(self, org_id: uuid.UUID) -> int:
        """Calls holding a concurrency slot now (expired members are dropped first)."""
        key = keys.concurrency_key(org_id)
        await self._redis.zremrangebyscore(key, "-inf", self._now_ms())
        return int(await self._redis.zcard(key))

    async def usage(self, org_id: uuid.UUID) -> LimitUsage:
        """The org's daily, hourly and international counters and its active calls."""
        now = self._clock.now()
        values = await self._redis.mget(
            [keys.daily_key(org_id, now), keys.hourly_key(org_id, now), keys.international_key(org_id, now)]
        )
        daily, hourly, international = (0 if value is None else int(value) for value in values)
        return LimitUsage(
            daily=daily,
            hourly=hourly,
            international=international,
            concurrency=await self.active_calls(org_id),
        )

    def _now_ms(self) -> int:
        return int(self._clock.now().timestamp() * 1000)

    async def _same_number(self, inputs: LimitInputs) -> None:
        key = keys.same_number_key(inputs.org_id, inputs.destination)
        if not await self._redis.set(key, 1, nx=True, px=SAME_NUMBER_WINDOW_MS):
            raise AppError("SAME_NUMBER_TOO_SOON", SAME_NUMBER_MESSAGE, field="phone_number")

    async def _frequency(self, inputs: LimitInputs, now: datetime, ticket: LimitTicket) -> None:
        key = keys.frequency_key(inputs.org_id, inputs.destination, now)
        cap = frequency_cap(inputs.limits, inputs.settings, inputs.destination)
        if not await self._take(key, cap, DAY_TTL_S, "frequency", ticket):
            raise AppError("DESTINATION_FREQUENCY_CAP", DESTINATION_FREQUENCY_MESSAGE, field="phone_number")

    async def _daily(self, inputs: LimitInputs, now: datetime, young: bool, ticket: LimitTicket) -> None:
        plan_cap = inputs.limits.daily_calls
        cap = _tightened(plan_cap, NEW_ACCOUNT_DAILY_CAP) if young else plan_cap
        result = await self._scripts.capped_incr(keys.daily_key(inputs.org_id, now), cap, DAY_TTL_S)
        if result.ok:
            ticket.add("daily", self._release(keys.daily_key(inputs.org_id, now)))
            return
        if young and (plan_cap is None or result.value < plan_cap):
            raise AppError("NEW_ACCOUNT_LIMIT", NEW_ACCOUNT_CAPS_MESSAGE)
        raise AppError("DAILY_CAP_REACHED", DAILY_CAP_MESSAGE)

    async def _counter(self, key: str, cap: int | None, ttl_s: int, label: str, ticket: LimitTicket) -> None:
        if not await self._take(key, cap, ttl_s, label, ticket):
            code, message = _COUNTER_ERRORS[label]
            raise AppError(code, message)

    async def _take(self, key: str, cap: int | None, ttl_s: int, label: str, ticket: LimitTicket) -> bool:
        result = await self._scripts.capped_incr(key, cap, ttl_s)
        if result.ok:
            ticket.add(label, self._release(key))
        return result.ok

    def _release(self, key: str) -> Undo:
        async def release() -> object:
            return await self._scripts.counter_release(key)

        return release

    def _forget(self, key: str, member: str) -> Undo:
        async def forget() -> object:
            return await self._redis.zrem(key, member)

        return forget

    async def _concurrency(
        self, inputs: LimitInputs, now: datetime, young: bool, ticket: LimitTicket
    ) -> None:
        plan_cap = _shared(inputs.limits.concurrency, inputs.concurrency_share)
        cap = (
            _shared(
                _tightened(inputs.limits.concurrency, NEW_ACCOUNT_CONCURRENCY_CAP), inputs.concurrency_share
            )
            if young
            else plan_cap
        )
        key = keys.concurrency_key(inputs.org_id)
        member = str(inputs.call_id)
        now_ms = int(now.timestamp() * 1000)
        expiry_ms = now_ms + (inputs.max_duration_min + CONCURRENCY_GRACE_MIN) * _MS_PER_MIN
        if await self._scripts.concurrency_acquire(key, member, cap, now_ms=now_ms, expiry_ms=expiry_ms):
            ticket.add("concurrency", self._forget(key, member))
            return
        if young and (plan_cap is None or (cap is not None and cap < plan_cap)):
            raise AppError("NEW_ACCOUNT_LIMIT", NEW_ACCOUNT_CAPS_MESSAGE)
        raise AppError("CONCURRENCY_CAP_REACHED", CONCURRENCY_CAP_MESSAGE)


_COUNTER_ERRORS: Final = {
    "hourly": ("HOURLY_CAP_REACHED", HOURLY_CAP_MESSAGE),
    "international": ("INTERNATIONAL_RATE_LIMITED", INTERNATIONAL_RATE_MESSAGE),
}
