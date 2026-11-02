"""Send Call step 10 (specs/24 §6, Decision P05-T04b): order, error codes, new-account rules, rollback."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import timedelta

import pytest
from billing_helpers import NOW, LimiterRig, another_call, inputs, random_e164

from va_billing import keys
from va_billing.limits import LimitInputs, LimitTicket, frequency_cap, is_new_account
from va_billing.plans import effective_limits
from va_common.errors import AppError
from va_common.ids import new_id
from va_domain.org import OrgSettings

YOUNG = NOW - timedelta(hours=2)


def settings(**compliance: object) -> OrgSettings:
    return OrgSettings.model_validate({"compliance": compliance})


async def expect(rig: LimiterRig, call: object, code: str, message: str, field: str | None = None) -> None:
    with pytest.raises(AppError) as caught:
        await rig.limiter.acquire(call)  # type: ignore[arg-type]
    assert (caught.value.code, caught.value.message, caught.value.field) == (code, message, field)
    assert caught.value.http_status == 429 or code == "SERVICE_UNAVAILABLE"


async def test_success_takes_every_counter(rig: LimiterRig) -> None:
    call = inputs()
    ticket = await rig.limiter.acquire(call)
    assert ticket.labels == ("frequency", "daily", "hourly", "concurrency")
    org = call.org_id
    assert await rig.value(keys.frequency_key(org, call.destination, NOW)) == 1
    assert await rig.value(keys.daily_key(org, NOW)) == 1
    assert await rig.value(keys.hourly_key(org, NOW)) == 1
    assert await rig.redis.zscore(keys.concurrency_key(org), str(call.call_id)) == (
        NOW.timestamp() * 1000 + 35 * 60_000
    )
    assert 0 < await rig.redis.pttl(keys.same_number_key(org, call.destination)) <= 10_000
    assert 0 < await rig.redis.ttl(keys.daily_key(org, NOW)) <= 172_800
    assert 0 < await rig.redis.ttl(keys.hourly_key(org, NOW)) <= 7_200


async def test_same_number_rule(rig: LimiterRig) -> None:
    call = inputs()
    await rig.limiter.acquire(call)
    message = "Please wait at least 10 seconds between calls to the same number."
    await expect(
        rig, another_call(call, destination=call.destination), "SAME_NUMBER_TOO_SOON", message, "phone_number"
    )
    assert await rig.value(keys.daily_key(call.org_id, NOW)) == 1


async def test_same_number_key_survives_a_later_failure(rig: LimiterRig) -> None:
    call = inputs(overrides={"daily_calls": 0})
    await expect(rig, call, "DAILY_CAP_REACHED", "Daily call limit reached for this organization.")
    assert await rig.redis.exists(keys.same_number_key(call.org_id, call.destination)) == 1
    assert await rig.value(keys.frequency_key(call.org_id, call.destination, NOW)) == 0


async def test_destination_frequency_cap(rig: LimiterRig) -> None:
    call = inputs()
    for _ in range(5):
        await rig.limiter.acquire(call)
        await rig.redis.delete(keys.same_number_key(call.org_id, call.destination))
        call = another_call(call, destination=call.destination)
    message = "This number has reached its daily call limit."
    await expect(rig, call, "DESTINATION_FREQUENCY_CAP", message, "phone_number")
    assert await rig.value(keys.daily_key(call.org_id, NOW)) == 5


def test_frequency_cap_sources() -> None:
    limits = effective_limits(None, {})
    florida = random_e164("305")
    assert frequency_cap(limits, OrgSettings(), florida) == 3
    assert frequency_cap(limits, settings(state_quiet_hours=False), florida) == 5
    assert frequency_cap(limits, OrgSettings(), random_e164()) == 5
    assert frequency_cap(limits, settings(max_attempts_per_number_per_day=2), random_e164()) == 2
    assert frequency_cap(limits, settings(max_attempts_per_number_per_day=9), random_e164()) == 5
    enterprise = replace(limits, per_destination_daily_calls=20)
    assert frequency_cap(enterprise, OrgSettings(), random_e164()) == 20
    assert frequency_cap(enterprise, OrgSettings(), florida) == 3


async def test_florida_number_capped_at_three(rig: LimiterRig) -> None:
    call = inputs(destination=random_e164("305"))
    for _ in range(3):
        await rig.limiter.acquire(call)
        await rig.redis.delete(keys.same_number_key(call.org_id, call.destination))
        call = another_call(call, destination=call.destination)
    with pytest.raises(AppError, match="daily call limit"):
        await rig.limiter.acquire(call)


def test_is_new_account() -> None:
    assert is_new_account(YOUNG, NOW)
    assert not is_new_account(NOW - timedelta(hours=24), NOW)


async def test_new_account_only_us_and_canada(rig: LimiterRig) -> None:
    message = "New accounts can call only US and Canadian numbers during their first 24 hours."
    call = inputs(created_at=YOUNG, region="GB", destination="+442071838750")
    await expect(rig, call, "NEW_ACCOUNT_LIMIT", message, "phone_number")
    assert await rig.value(keys.frequency_key(call.org_id, call.destination, NOW)) == 0
    await rig.limiter.acquire(inputs(created_at=YOUNG, region="CA", destination=random_e164("416")))


async def test_new_account_daily_tightening(rig: LimiterRig) -> None:
    call = inputs(created_at=YOUNG)
    await rig.redis.set(keys.daily_key(call.org_id, NOW), 100)
    message = (
        "New accounts are limited to 100 calls per day and 10 concurrent calls during their first 24 hours."
    )
    await expect(rig, call, "NEW_ACCOUNT_LIMIT", message)
    unlimited = inputs(plan_id="enterprise", created_at=YOUNG)
    await rig.redis.set(keys.daily_key(unlimited.org_id, NOW), 100)
    await expect(rig, unlimited, "NEW_ACCOUNT_LIMIT", message)


async def test_new_account_below_plan_cap_reports_daily_cap(rig: LimiterRig) -> None:
    call = inputs(created_at=YOUNG, overrides={"daily_calls": 50})
    await rig.redis.set(keys.daily_key(call.org_id, NOW), 50)
    await expect(rig, call, "DAILY_CAP_REACHED", "Daily call limit reached for this organization.")


async def test_new_account_concurrency_tightening(rig: LimiterRig) -> None:
    base = inputs(created_at=YOUNG)
    for _ in range(10):
        await rig.limiter.acquire(another_call(base))
    message = (
        "New accounts are limited to 100 calls per day and 10 concurrent calls during their first 24 hours."
    )
    await expect(rig, another_call(base), "NEW_ACCOUNT_LIMIT", message)
    unlimited = inputs(plan_id="enterprise", created_at=YOUNG)
    for _ in range(10):
        await rig.limiter.acquire(another_call(unlimited))
    await expect(rig, another_call(unlimited), "NEW_ACCOUNT_LIMIT", message)


async def test_new_account_with_small_plan_reports_concurrency_cap(rig: LimiterRig) -> None:
    base = inputs(created_at=YOUNG, overrides={"concurrency": 2})
    for _ in range(2):
        await rig.limiter.acquire(another_call(base))
    await expect(
        rig,
        another_call(base),
        "CONCURRENCY_CAP_REACHED",
        "Concurrent call limit reached for this organization.",
    )


async def test_daily_cap_rolls_at_utc_midnight(rig: LimiterRig) -> None:
    base = inputs(overrides={"daily_calls": 1})
    await rig.limiter.acquire(base)
    await expect(
        rig, another_call(base), "DAILY_CAP_REACHED", "Daily call limit reached for this organization."
    )
    rig.clock.set(NOW.replace(hour=23, minute=59))
    await expect(
        rig, another_call(base), "DAILY_CAP_REACHED", "Daily call limit reached for this organization."
    )
    rig.clock.set(NOW + timedelta(days=1) - timedelta(hours=10, minutes=15))
    await rig.limiter.acquire(another_call(base))


async def test_hourly_cap_and_rollback(rig: LimiterRig) -> None:
    base = inputs(overrides={"hourly_calls": 1})
    await rig.limiter.acquire(base)
    blocked = another_call(base)
    await expect(rig, blocked, "HOURLY_CAP_REACHED", "Hourly call limit reached for this organization.")
    assert await rig.value(keys.daily_key(base.org_id, NOW)) == 1
    assert await rig.value(keys.frequency_key(base.org_id, blocked.destination, NOW)) == 0
    rig.clock.advance(timedelta(hours=1))
    await rig.limiter.acquire(another_call(base))


async def test_concurrency_cap_rolls_back_earlier_counters(rig: LimiterRig) -> None:
    base = inputs(overrides={"concurrency": 1})
    await rig.limiter.acquire(base)
    blocked = another_call(base)
    await expect(
        rig, blocked, "CONCURRENCY_CAP_REACHED", "Concurrent call limit reached for this organization."
    )
    assert await rig.value(keys.daily_key(base.org_id, NOW)) == 1
    assert await rig.value(keys.hourly_key(base.org_id, NOW)) == 1
    assert await rig.value(keys.frequency_key(base.org_id, blocked.destination, NOW)) == 0
    await rig.limiter.release_call(base.org_id, base.call_id)
    await rig.limiter.release_call(base.org_id, base.call_id)
    await rig.limiter.acquire(another_call(base))


async def test_concurrency_slot_expires_after_max_duration(rig: LimiterRig) -> None:
    base = inputs(overrides={"concurrency": 1}, max_duration_min=10)
    await rig.limiter.acquire(base)
    rig.clock.advance(timedelta(minutes=15, seconds=1))
    await rig.limiter.acquire(another_call(base))
    assert await rig.limiter.active_calls(base.org_id) == 1


async def test_concurrency_share(rig: LimiterRig) -> None:
    base = inputs(overrides={"concurrency": 4}, concurrency_share=0.5)
    for _ in range(2):
        await rig.limiter.acquire(another_call(base))
    await expect(
        rig,
        another_call(base),
        "CONCURRENCY_CAP_REACHED",
        "Concurrent call limit reached for this organization.",
    )
    tiny = inputs(overrides={"concurrency": 1}, concurrency_share=0.1)
    await rig.limiter.acquire(tiny)


async def test_international_counter(rig: LimiterRig) -> None:
    base = inputs(plan_id="start", region="GB", destination="+442071838750", overrides={"concurrency": 100})
    for _ in range(10):
        await rig.limiter.acquire(another_call(base, destination=f"+4420718{_:05d}"))
    blocked = another_call(base, destination="+442071899999")
    message = "International call limit per hour reached for this organization."
    await expect(rig, blocked, "INTERNATIONAL_RATE_LIMITED", message)
    assert await rig.value(keys.international_key(base.org_id, NOW)) == 10
    assert await rig.value(keys.daily_key(base.org_id, NOW)) == 10
    assert await rig.limiter.active_calls(base.org_id) == 10


async def test_domestic_calls_skip_the_international_counter(rig: LimiterRig) -> None:
    canada = inputs(region="CA", destination=random_e164("416"))
    await rig.limiter.acquire(canada)
    home = inputs(region="MX", destination="+525512345678", settings=OrgSettings(default_country="MX"))
    ticket = await rig.limiter.acquire(home)
    assert "international" not in ticket.labels
    assert await rig.redis.exists(keys.international_key(canada.org_id, NOW)) == 0


async def test_unlimited_caps_still_count(rig: LimiterRig) -> None:
    base = inputs(plan_id="enterprise", region="GB", destination="+442071838750")
    await rig.limiter.acquire(base)
    await rig.limiter.acquire(another_call(base, destination="+442071838751"))
    usage = await rig.limiter.usage(base.org_id)
    assert (usage.daily, usage.hourly, usage.international, usage.concurrency) == (2, 2, 2, 2)
    empty = await rig.limiter.usage(inputs().org_id)
    assert (empty.daily, empty.hourly, empty.international, empty.concurrency) == (0, 0, 0, 0)


async def test_ticket_rollback_is_idempotent(rig: LimiterRig) -> None:
    call = inputs(region="GB", destination="+442071838750", overrides={"international_enabled": True})
    ticket = await rig.limiter.acquire(call)
    await ticket.rollback()
    await ticket.rollback()
    assert ticket.labels == ()
    org = call.org_id
    assert await rig.value(keys.daily_key(org, NOW)) == 0
    assert await rig.value(keys.hourly_key(org, NOW)) == 0
    assert await rig.value(keys.international_key(org, NOW)) == 0
    assert await rig.value(keys.frequency_key(org, call.destination, NOW)) == 0
    assert await rig.limiter.active_calls(org) == 0


async def test_redis_down_fails_closed_and_leaves_nothing(rig: LimiterRig) -> None:
    call = inputs()
    rig.server.connected = False
    await expect(rig, call, "SERVICE_UNAVAILABLE", "Call limits are temporarily unavailable.")
    rig.server.connected = True
    assert await rig.redis.dbsize() == 0


async def test_redis_failure_midway_rolls_back(rig: LimiterRig, monkeypatch: pytest.MonkeyPatch) -> None:
    call = inputs()
    scripts = rig.limiter._scripts
    original = scripts.concurrency_acquire

    async def broken(*args: object, **kwargs: object) -> bool:
        from redis.exceptions import ConnectionError as RedisConnectionError  # noqa: PLC0415

        raise RedisConnectionError("gone")

    monkeypatch.setattr(scripts, "concurrency_acquire", broken)
    await expect(rig, call, "SERVICE_UNAVAILABLE", "Call limits are temporarily unavailable.")
    assert await rig.value(keys.daily_key(call.org_id, NOW)) == 0
    assert await rig.value(keys.hourly_key(call.org_id, NOW)) == 0
    monkeypatch.setattr(scripts, "concurrency_acquire", original)


async def test_rollback_logs_and_continues_on_redis_errors(rig: LimiterRig) -> None:
    calls: list[str] = []

    async def failing() -> object:
        from redis.exceptions import ConnectionError as RedisConnectionError  # noqa: PLC0415

        calls.append("failing")
        raise RedisConnectionError("gone")

    async def working() -> object:
        calls.append("working")
        return 1

    ticket = LimitTicket()
    ticket.add("first", working)
    ticket.add("second", failing)
    await ticket.rollback()
    assert calls == ["failing", "working"]


# ---- inbound calls (specs/06 §7 step 7) -------------------------------------------------------------------


async def inbound(rig: LimiterRig, base: LimitInputs, call_id: uuid.UUID | None = None) -> bool:
    return await rig.limiter.acquire_inbound(
        base.org_id,
        call_id or new_id(),
        limits=base.limits,
        org_created_at=base.org_created_at,
        max_duration_min=base.max_duration_min,
    )


async def test_inbound_takes_only_a_concurrency_slot(rig: LimiterRig) -> None:
    base = inputs(overrides={"concurrency": 2})
    call_id = new_id()
    assert await inbound(rig, base, call_id)
    assert await inbound(rig, base, call_id)
    assert await rig.limiter.active_calls(base.org_id) == 1
    assert await rig.value(keys.daily_key(base.org_id, NOW)) is None
    assert await rig.value(keys.hourly_key(base.org_id, NOW)) is None
    assert await inbound(rig, base)
    assert not await inbound(rig, base)
    await rig.limiter.release_call(base.org_id, call_id)
    assert await inbound(rig, base)


async def test_inbound_shares_the_cap_with_outbound_calls(rig: LimiterRig) -> None:
    base = inputs(overrides={"concurrency": 1})
    await rig.limiter.acquire(base)
    assert not await inbound(rig, base)


async def test_inbound_new_account_tightening(rig: LimiterRig) -> None:
    base = inputs(plan_id="enterprise", created_at=YOUNG)
    assert all([await inbound(rig, base) for _ in range(10)])
    assert not await inbound(rig, base)
    unlimited = inputs(plan_id="enterprise")
    assert all([await inbound(rig, unlimited) for _ in range(20)])


async def test_inbound_slot_expires_after_max_duration(rig: LimiterRig) -> None:
    base = inputs(overrides={"concurrency": 1}, max_duration_min=10)
    assert await inbound(rig, base)
    rig.clock.advance(timedelta(minutes=15, seconds=1))
    assert await inbound(rig, base)
    assert await rig.limiter.active_calls(base.org_id) == 1


async def test_inbound_redis_down_is_service_unavailable(rig: LimiterRig) -> None:
    rig.server.connected = False
    with pytest.raises(AppError) as caught:
        await inbound(rig, inputs())
    assert caught.value.code == "SERVICE_UNAVAILABLE"
