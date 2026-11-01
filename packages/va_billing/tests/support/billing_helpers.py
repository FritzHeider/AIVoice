"""Builders for the va_billing tests: seed-like plans, limit inputs, a limiter on fakeredis."""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from fakeredis import FakeAsyncRedis, FakeServer

from va_billing.limits import CallLimiter, LimitInputs
from va_billing.plans import effective_limits
from va_common.ids import new_id
from va_common.time import FrozenClock
from va_db.models import Plan
from va_domain.org import OrgSettings

__all__ = [
    "NOW",
    "PLAN_ROWS",
    "LimiterRig",
    "another_call",
    "inputs",
    "limiter_rig",
    "make_plan",
    "random_e164",
]

# 2026-11-02 10:15 UTC: a Monday, mid-hour, so day/hour keys are unambiguous.
NOW = datetime(2026, 11, 2, 10, 15, tzinfo=UTC)

PLAN_ROWS: dict[str, dict[str, Any]] = {
    "start": {
        "connected": 120_000,
        "transfer": 50_000,
        "daily": 100,
        "hourly": 100,
        "concurrency": 10,
        "clones": 1,
    },
    "build": {
        "connected": 100_000,
        "transfer": 40_000,
        "daily": 2000,
        "hourly": 1000,
        "concurrency": 50,
        "clones": 5,
    },
    "scale": {
        "connected": 90_000,
        "transfer": 30_000,
        "daily": 5000,
        "hourly": 1000,
        "concurrency": 100,
        "clones": 15,
    },
    "enterprise": {
        "connected": 80_000,
        "transfer": 25_000,
        "daily": None,
        "hourly": None,
        "concurrency": None,
        "clones": None,
    },
}


def make_plan(plan_id: str = "build", **values: Any) -> Plan:
    """A ``Plan`` with the specs/24 §2 seed values of ``plan_id`` (``values`` override attributes)."""
    row = PLAN_ROWS[plan_id]
    attributes: dict[str, Any] = {
        "id": plan_id,
        "name": plan_id.title(),
        "monthly_fee_micros": 0,
        "connected_rate_micros_per_min": row["connected"],
        "transfer_rate_micros_per_min": row["transfer"],
        "min_attempt_micros": 15_000,
        "sms_rate_micros": 20_000,
        "widget_message_micros": 10_000,
        "tts_rate_micros_per_1k_chars": 120_000,
        "daily_call_cap": row["daily"],
        "hourly_call_cap": row["hourly"],
        "concurrency_cap": row["concurrency"],
        "voice_clone_cap": row["clones"],
    }
    return Plan(**(attributes | values))


def random_e164(npa: str = "212") -> str:
    """A random NANP number in area code ``npa`` (212 = New York, which has no state rule)."""
    return f"+1{npa}{secrets.randbelow(10**7):07d}"


def inputs(  # noqa: PLR0913 - test builder with keyword defaults
    *,
    plan_id: str = "build",
    overrides: dict[str, object] | None = None,
    settings: OrgSettings | None = None,
    destination: str | None = None,
    region: str | None = "US",
    created_at: datetime | None = None,
    org_id: uuid.UUID | None = None,
    max_duration_min: int = 30,
    concurrency_share: float = 1.0,
) -> LimitInputs:
    """Limit inputs of an established org (created 30 days before ``NOW``) calling a fresh NY number."""
    return LimitInputs(
        org_id=org_id or new_id(),
        org_created_at=created_at or NOW - timedelta(days=30),
        limits=effective_limits(make_plan(plan_id), overrides or {}),
        settings=settings or OrgSettings(),
        destination=destination or random_e164(),
        region=region,
        call_id=new_id(),
        max_duration_min=max_duration_min,
        concurrency_share=concurrency_share,
    )


def another_call(base: LimitInputs, *, destination: str | None = None) -> LimitInputs:
    """``base`` with a new call id and (by default) a fresh destination."""
    return replace(base, call_id=new_id(), destination=destination or random_e164())


@dataclass
class LimiterRig:
    server: FakeServer
    redis: FakeAsyncRedis
    clock: FrozenClock
    limiter: CallLimiter

    async def value(self, key: str) -> int | None:
        raw = await self.redis.get(key)
        return None if raw is None else int(raw)


def limiter_rig(now: datetime = NOW) -> LimiterRig:
    server = FakeServer()
    redis = FakeAsyncRedis(server=server)
    clock = FrozenClock(now)
    return LimiterRig(server=server, redis=redis, clock=clock, limiter=CallLimiter(redis, clock=clock))
