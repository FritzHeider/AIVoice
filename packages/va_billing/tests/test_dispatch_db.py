"""Send Call steps 9-11 together (specs/05 §4.1, Decision P05-T04b): order and compensation."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from billing_helpers import NOW, LimiterRig, random_e164
from billing_rows import holds_of, new_billing_org
from compliance_rows import add_dnc

from va_billing import keys
from va_billing.dispatch import Admitted, CallAdmission, admit_call, load_billing_org
from va_common.errors import AppError
from va_common.ids import new_id
from va_compliance.international import parse_allowed_countries
from va_db.session import Database

pytestmark = [pytest.mark.integration, pytest.mark.timeout(240)]

ALLOWED = parse_allowed_countries("US,CA,GB")


def admission(
    destination: str | None = None, *, scheduled: bool = False, region: str = "US", minutes: int = 10
) -> CallAdmission:
    return CallAdmission(
        call_id=new_id(),
        destination=destination or random_e164(),
        region=region,
        max_duration_min=minutes,
        at=NOW + timedelta(days=2) if scheduled else NOW,
        scheduled=scheduled,
        allowed_countries=ALLOWED,
    )


async def admit(database: Database, rig: LimiterRig, org_id: uuid.UUID, call: CallAdmission) -> Admitted:
    async with database.tenant_session(org_id) as session:
        org = await load_billing_org(session, org_id)
        return await admit_call(session, rig.limiter, org, call, now=NOW)


async def counters(rig: LimiterRig, org_id: uuid.UUID) -> tuple[int, int, int, int]:
    usage = await rig.limiter.usage(org_id)
    return usage.daily, usage.hourly, usage.international, usage.concurrency


async def test_success_takes_limits_and_a_hold(database: Database, rig: LimiterRig) -> None:
    org_id = await new_billing_org(
        database, balance=5_000_000, limits={"connected_rate_micros_per_min": 50_000}
    )
    call = admission()
    admitted = await admit(database, rig, org_id, call)
    assert admitted.pricing.connected_rate_micros_per_min == 50_000
    assert admitted.hold is not None
    assert admitted.hold.amount_micros == 10 * 50_000 + 15_000
    assert admitted.ticket is not None
    assert admitted.ticket.labels == ("frequency", "daily", "hourly", "concurrency")
    assert await counters(rig, org_id) == (1, 1, 0, 1)
    async with database.tenant_session(org_id) as session:
        await admitted.compensate(session, rig.limiter)
        await admitted.compensate(session, rig.limiter)
    assert await counters(rig, org_id) == (0, 0, 0, 0)
    assert [row.status for row in await holds_of(database, org_id)] == ["released"]


async def test_compliance_failure_touches_nothing(database: Database, rig: LimiterRig) -> None:
    org_id = await new_billing_org(database, balance=5_000_000)
    blocked = random_e164()
    await add_dnc(database, org_id, blocked)
    with pytest.raises(AppError) as caught:
        await admit(database, rig, org_id, admission(blocked))
    assert caught.value.code == "NUMBER_BLOCKED"
    assert await rig.redis.dbsize() == 0
    assert await holds_of(database, org_id) == []


async def test_limit_failure_takes_no_hold(database: Database, rig: LimiterRig) -> None:
    org_id = await new_billing_org(database, balance=5_000_000, limits={"hourly_calls": 0})
    with pytest.raises(AppError) as caught:
        await admit(database, rig, org_id, admission())
    assert caught.value.code == "HOURLY_CAP_REACHED"
    assert await holds_of(database, org_id) == []
    assert await counters(rig, org_id) == (0, 0, 0, 0)


async def test_hold_failure_rolls_the_counters_back(database: Database, rig: LimiterRig) -> None:
    org_id = await new_billing_org(
        database,
        balance=1_000,
        plan_id="scale",
        limits={"international_enabled": True},
        settings={"international_enabled": True, "allowed_countries": ["GB"]},
    )
    call = admission("+442071838750", region="GB")
    with pytest.raises(AppError) as caught:
        await admit(database, rig, org_id, call)
    assert caught.value.code == "INSUFFICIENT_CREDITS"
    assert await counters(rig, org_id) == (0, 0, 0, 0)
    assert await rig.redis.exists(keys.international_key(org_id, NOW)) == 1
    assert await rig.value(keys.frequency_key(org_id, call.destination, NOW)) == 0
    assert await holds_of(database, org_id) == []


async def test_scheduled_calls_run_only_compliance(database: Database, rig: LimiterRig) -> None:
    org_id = await new_billing_org(database)
    admitted = await admit(database, rig, org_id, admission(scheduled=True))
    assert (admitted.hold, admitted.ticket) == (None, None)
    assert await rig.redis.dbsize() == 0
    async with database.tenant_session(org_id) as session:
        await admitted.compensate(session, rig.limiter)


async def test_skip_holds_org(database: Database, rig: LimiterRig) -> None:
    org_id = await new_billing_org(database, plan_id="enterprise", credit_limit=1_000_000_000)
    admitted = await admit(database, rig, org_id, admission())
    assert admitted.hold is None
    assert admitted.ticket is not None


async def test_load_billing_org(database: Database) -> None:
    org_id = await new_billing_org(
        database,
        plan_id="start",
        credit_limit=7,
        limits={"daily_calls": 3},
        settings={"default_country": "GB"},
        created_at=NOW - timedelta(hours=1),
    )
    async with database.tenant_session(org_id) as session:
        org = await load_billing_org(session, org_id)
    assert (org.plan_id, org.credit_limit_micros, org.created_at) == ("start", 7, NOW - timedelta(hours=1))
    assert org.limits.daily_calls == 3
    assert org.limits.concurrency == 10
    assert org.settings.default_country == "GB"
    assert org.pricing.connected_rate_micros_per_min == 120_000
    assert org.facts().limits == {"daily_calls": 3}


async def test_load_billing_org_errors(database: Database) -> None:
    missing = new_id()
    async with database.tenant_session(missing) as session:
        with pytest.raises(AppError, match="Organization not found"):
            await load_billing_org(session, missing)
    unknown_plan = await new_billing_org(database, plan_id="vanished")
    async with database.tenant_session(unknown_plan) as session:
        with pytest.raises(AppError) as caught:
            await load_billing_org(session, unknown_plan)
    assert (caught.value.code, caught.value.message) == (
        "SERVICE_UNAVAILABLE",
        "Billing is not configured for this organization.",
    )
