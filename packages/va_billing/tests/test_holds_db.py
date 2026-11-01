"""Credit holds (specs/24 §5): amount, available balance, insufficient credits, skip, capture/release."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from billing_helpers import NOW, make_plan
from billing_rows import holds_of, new_billing_org

from va_billing import holds
from va_billing.plans import effective_pricing
from va_common.errors import AppError
from va_common.ids import new_id
from va_db.models import CreditHold
from va_db.session import Database

pytestmark = [pytest.mark.integration, pytest.mark.timeout(240)]

PRICING = effective_pricing(make_plan("build"), {})  # $0.10/min connected, $0.015 min attempt


def test_call_hold_amount() -> None:
    assert holds.call_hold_amount(3, PRICING) == 3 * 100_000 + 15_000
    assert holds.call_hold_amount(30, PRICING) == 10 * 100_000 + 15_000
    assert holds.call_hold_amount(10, PRICING) == 1_015_000


def test_holds_skipped() -> None:
    assert holds.holds_skipped(1_000_000_000)
    assert not holds.holds_skipped(999_999_999)


async def reserve_call(
    database: Database, org_id: uuid.UUID, *, minutes: int = 30, credit_limit: int = 0
) -> CreditHold | None:
    async with database.tenant_session(org_id) as session:
        return await holds.reserve_call_hold(
            session,
            org_id,
            new_id(),
            credit_limit_micros=credit_limit,
            max_duration_min=minutes,
            pricing=PRICING,
            now=NOW,
        )


async def test_reserve_inserts_a_held_row_and_keeps_the_balance(database: Database) -> None:
    org_id = await new_billing_org(database, balance=2_000_000)
    call_id = new_id()
    async with database.tenant_session(org_id) as session:
        hold = await holds.reserve_call_hold(
            session, org_id, call_id, credit_limit_micros=0, max_duration_min=5, pricing=PRICING, now=NOW
        )
        assert hold is not None
        assert await holds.available_balance(session, org_id) == 2_000_000 - 515_000
    (row,) = await holds_of(database, org_id)
    assert (row.status, row.amount_micros, row.reference_type, row.reference_id) == (
        "held",
        515_000,
        "call",
        str(call_id),
    )
    assert row.expires_at == NOW + timedelta(minutes=5, hours=2)


async def test_available_balance_counts_holds_and_credit_limit(database: Database) -> None:
    org_id = await new_billing_org(database, balance=500_000, credit_limit=1_000_000)
    await reserve_call(database, org_id, minutes=10)  # 1 015 000
    async with database.tenant_session(org_id) as session:
        assert await holds.available_balance(session, org_id) == 500_000 - 1_015_000 + 1_000_000
    with pytest.raises(AppError) as caught:
        await reserve_call(database, org_id, minutes=10)
    assert (caught.value.code, caught.value.message, caught.value.http_status) == (
        "INSUFFICIENT_CREDITS",
        "Insufficient credits to place this call.",
        402,
    )
    assert len(await holds_of(database, org_id)) == 1


async def test_exact_available_amount_is_enough(database: Database) -> None:
    org_id = await new_billing_org(database, balance=315_000)
    assert await reserve_call(database, org_id, minutes=3) is not None


async def test_skip_holds_for_large_credit_limits(database: Database) -> None:
    org_id = await new_billing_org(database, credit_limit=1_000_000_000)
    assert await reserve_call(database, org_id, credit_limit=1_000_000_000) is None
    assert await holds_of(database, org_id) == []


async def test_capture_and_release_are_idempotent(database: Database) -> None:
    org_id = await new_billing_org(database, balance=10_000_000)
    first = await reserve_call(database, org_id)
    second = await reserve_call(database, org_id)
    assert first is not None
    assert second is not None
    async with database.tenant_session(org_id) as session:
        assert await holds.capture(session, org_id, first.id)
        assert not await holds.capture(session, org_id, first.id)
        assert not await holds.release(session, org_id, first.id)
        assert await holds.release(session, org_id, second.id)
        assert not await holds.release(session, org_id, second.id)
        assert not await holds.capture(session, org_id, second.id)
        assert not await holds.release(session, org_id, new_id())
        assert await holds.available_balance(session, org_id) == 10_000_000
    assert [row.status for row in await holds_of(database, org_id)] == ["captured", "released"]


async def test_capture_and_release_filter_by_org(database: Database) -> None:
    org_id = await new_billing_org(database, balance=10_000_000)
    other_id = await new_billing_org(database, balance=10_000_000)
    hold = await reserve_call(database, org_id)
    assert hold is not None
    async with database.tenant_session(org_id) as session:
        assert not await holds.capture(session, other_id, hold.id)
        assert not await holds.release(session, other_id, hold.id)
    assert [row.status for row in await holds_of(database, org_id)] == ["held"]


async def test_concurrent_reserves_for_the_last_credit(database: Database) -> None:
    org_id = await new_billing_org(database, balance=1_015_000)
    results = await asyncio.gather(
        reserve_call(database, org_id, minutes=10),
        reserve_call(database, org_id, minutes=10),
        return_exceptions=True,
    )
    errors = [result for result in results if isinstance(result, AppError)]
    assert len(errors) == 1
    assert errors[0].code == "INSUFFICIENT_CREDITS"
    assert len(await holds_of(database, org_id)) == 1


async def test_unknown_org_and_negative_amount(database: Database) -> None:
    missing = new_id()
    async with database.tenant_session(missing) as session:
        with pytest.raises(AppError, match="Organization not found"):
            await holds.reserve(session, missing, 1, reference_type="call", reference_id="x", expires_at=NOW)
        with pytest.raises(AppError, match="Organization not found"):
            await holds.available_balance(session, missing)
        with pytest.raises(ValueError, match="negative"):
            await holds.reserve(session, missing, -1, reference_type="call", reference_id="x", expires_at=NOW)
