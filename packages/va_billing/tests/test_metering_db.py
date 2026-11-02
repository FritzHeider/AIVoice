"""``finalize_call`` on Postgres (specs/24 §3-§5): usage + ledger rows, balance, price, hold, idempotency."""

from __future__ import annotations

import asyncio
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from billing_rows import holds_of, new_billing_org
from call_rows import SeededCall, load_call, seed_call, seed_leg
from sms_rows import add_telnyx_account
from sqlalchemy import func, select

from va_billing import holds
from va_billing.metering import CallNotEndedError, FinalizedCall, finalize_call
from va_billing.plans import EffectivePricing
from va_common.errors import AppError
from va_common.ids import new_id
from va_db.models import CallDirection, CallStatus, CreditLedger, Org, UsageEvent
from va_db.repositories import grant_credits
from va_db.session import Database

pytestmark = [pytest.mark.integration, pytest.mark.timeout(240)]

PRICING = EffectivePricing(
    connected_rate_micros_per_min=120_000,
    transfer_rate_micros_per_min=60_000,
    min_attempt_micros=15_000,
    sms_rate_micros=20_000,
    widget_message_micros=10_000,
    tts_rate_micros_per_1k_chars=120_000,
)
START_BALANCE = 5_000_000


async def funded_org(database: Database, *, limits: dict[str, Any] | None = None) -> uuid.UUID:
    """An org whose whole starting balance is one ledger ``grant`` (so the ledger sums to the balance)."""
    org_id = await new_billing_org(database, limits=limits)
    async with database.tenant_session(org_id) as session:
        await grant_credits(session, org_id, START_BALANCE, idempotency_key="test:start")
    return org_id


async def held_call_hold(database: Database, org_id: uuid.UUID, call_id: uuid.UUID) -> uuid.UUID:
    async with database.tenant_session(org_id) as session:
        hold = await holds.reserve_call_hold(
            session,
            org_id,
            call_id,
            credit_limit_micros=0,
            max_duration_min=5,
            pricing=PRICING,
            now=datetime.now(UTC),
        )
        assert hold is not None
        return hold.id


async def ended_call(  # noqa: PLR0913 - keyword-only call facts
    database: Database,
    org_id: uuid.UUID,
    *,
    status: CallStatus = "completed",
    connected: str = "0",
    transfer: str = "0",
    direction: CallDirection = "outbound",
    dialed: bool = True,
    hold: bool = True,
    pricing: dict[str, Any] | None = None,
    leg_values: dict[str, Any] | None = None,
) -> SeededCall:
    call_id = new_id()
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "id": call_id,
        "direction": direction,
        "config": {"pricing": PRICING.snapshot()} if pricing is None else pricing,
        "connected_seconds": Decimal(connected),
        "transfer_seconds": Decimal(transfer),
        "ended_at": now,
    }
    if Decimal(connected) > 0:
        values["answered_at"] = now - timedelta(seconds=float(connected) + float(transfer))
    if hold:
        values["credit_hold_id"] = await held_call_hold(database, org_id, call_id)
    call = await seed_call(database, status=status, org_id=org_id, **values)
    if dialed:
        await seed_leg(database, call, f"v3:{secrets.token_hex(8)}", state="hangup", **(leg_values or {}))
    return call


async def finalize(database: Database, call: SeededCall) -> FinalizedCall:
    async with database.tenant_session(call.org_id) as session:
        return await finalize_call(session, call.org_id, call.call_id, now=datetime.now(UTC))


async def usage_of(database: Database, call: SeededCall) -> dict[str, tuple[Decimal, int, int]]:
    async with database.tenant_session(call.org_id) as session:
        rows = await session.scalars(select(UsageEvent).where(UsageEvent.reference_id == str(call.call_id)))
        return {row.meter: (row.quantity, row.unit_price_micros, row.amount_micros) for row in rows.all()}


async def ledger_of(database: Database, org_id: uuid.UUID) -> list[CreditLedger]:
    async with database.tenant_session(org_id) as session:
        rows = await session.scalars(
            select(CreditLedger)
            .where(CreditLedger.org_id == org_id)
            .order_by(CreditLedger.created_at, CreditLedger.balance_after_micros.desc())
        )
        return list(rows.all())


async def balance_of(database: Database, org_id: uuid.UUID) -> int:
    async with database.tenant_session(org_id) as session:
        org = await session.get_one(Org, org_id)
        return org.credits_balance_micros


async def assert_ledger_matches_balance(database: Database, org_id: uuid.UUID) -> None:
    async with database.tenant_session(org_id) as session:
        total = await session.scalar(
            select(func.coalesce(func.sum(CreditLedger.amount_micros), 0)).where(
                CreditLedger.org_id == org_id
            )
        )
    assert await balance_of(database, org_id) == total


async def test_connected_call_posts_usage_charges_and_captures(database: Database) -> None:
    org_id = await funded_org(database)
    call = await ended_call(database, org_id, connected="61.437")

    result = await finalize(database, call)

    assert result.price_micros == 122_874
    assert result.hold == "captured"
    assert result.posted == (f"call:{call.call_id}:connected", f"call:{call.call_id}:attempt")
    assert result.balance_after_micros == START_BALANCE - 122_874
    assert await usage_of(database, call) == {
        "call_connected_seconds": (Decimal("61.437000"), 2_000, 122_874),
        "call_attempt": (Decimal("1.000000"), 15_000, 0),
    }
    entries = [e for e in await ledger_of(database, org_id) if e.entry_type == "usage"]
    assert [(e.amount_micros, e.balance_after_micros) for e in entries] == [
        (-122_874, START_BALANCE - 122_874)
    ]
    (entry,) = entries
    assert entry.idempotency_key == f"call:{call.call_id}:connected"
    assert (entry.reference_type, entry.reference_id, entry.description) == (
        "call",
        str(call.call_id),
        "call_connected_seconds",
    )
    assert (await load_call(database, call)).price_micros == 122_874
    (hold,) = await holds_of(database, org_id)
    assert hold.status == "captured"
    assert await balance_of(database, org_id) == START_BALANCE - 122_874
    await assert_ledger_matches_balance(database, org_id)


async def test_usage_rows_occur_at_the_call_end(database: Database) -> None:
    org_id = await funded_org(database)
    call = await ended_call(database, org_id, connected="5")
    ended_at = (await load_call(database, call)).ended_at
    await finalize(database, call)
    async with database.tenant_session(org_id) as session:
        times = set(
            (
                await session.scalars(
                    select(UsageEvent.occurred_at).where(UsageEvent.reference_id == str(call.call_id))
                )
            ).all()
        )
    assert times == {ended_at}


async def test_finalize_twice_posts_nothing_new(database: Database) -> None:
    org_id = await funded_org(database)
    call = await ended_call(database, org_id, connected="3")
    first = await finalize(database, call)
    second = await finalize(database, call)

    assert first.price_micros == second.price_micros == 15_000
    assert second.posted == ()
    assert second.hold == "settled"
    assert second.balance_after_micros == first.balance_after_micros == START_BALANCE - 15_000
    assert len([e for e in await ledger_of(database, org_id) if e.entry_type == "usage"]) == 2
    await assert_ledger_matches_balance(database, org_id)


async def test_concurrent_finalizes_charge_once(database: Database) -> None:
    org_id = await funded_org(database)
    call = await ended_call(database, org_id, connected="10")

    results = await asyncio.gather(finalize(database, call), finalize(database, call))

    assert sorted(len(r.posted) for r in results) == [0, 2]
    assert {r.price_micros for r in results} == {20_000}
    assert await balance_of(database, org_id) == START_BALANCE - 20_000
    assert len(await usage_of(database, call)) == 2
    await assert_ledger_matches_balance(database, org_id)


async def test_transferred_call_posts_the_transfer_meter(database: Database) -> None:
    org_id = await funded_org(database)
    call = await ended_call(database, org_id, connected="30", transfer="90")

    result = await finalize(database, call)

    assert result.price_micros == 60_000 + 90_000
    usage = await usage_of(database, call)
    assert usage["call_transfer_seconds"] == (Decimal("90.000000"), 1_000, 90_000)
    await assert_ledger_matches_balance(database, org_id)


async def test_byo_transfer_is_free(database: Database) -> None:
    org_id = await funded_org(database)
    account_id = await add_telnyx_account(database, org_id, None)
    call = await ended_call(
        database, org_id, connected="30", transfer="90", leg_values={"telnyx_account_id": account_id}
    )

    result = await finalize(database, call)

    assert result.price_micros == 60_000
    assert (await usage_of(database, call))["call_transfer_seconds"] == (Decimal("90.000000"), 0, 0)


@pytest.mark.parametrize("status", ["no_answer", "busy", "failed"])
async def test_unanswered_dialed_call_costs_the_attempt_minimum(
    database: Database, status: CallStatus
) -> None:
    org_id = await funded_org(database)
    call = await ended_call(database, org_id, status=status)

    result = await finalize(database, call)

    assert result.price_micros == 15_000
    assert result.hold == "captured"
    assert await usage_of(database, call) == {"call_attempt": (Decimal("1.000000"), 15_000, 15_000)}
    await assert_ledger_matches_balance(database, org_id)


async def test_call_canceled_before_dial_costs_nothing_and_releases(database: Database) -> None:
    org_id = await funded_org(database)
    call = await ended_call(database, org_id, status="canceled", dialed=False)

    result = await finalize(database, call)

    assert result == FinalizedCall(
        price_micros=0, posted=(), hold="released", balance_after_micros=START_BALANCE
    )
    assert await usage_of(database, call) == {}
    (hold,) = await holds_of(database, org_id)
    assert hold.status == "released"


async def test_inbound_call_without_hold(database: Database) -> None:
    org_id = await funded_org(database)
    call = await ended_call(database, org_id, connected="3", direction="inbound", hold=False)

    result = await finalize(database, call)

    assert (result.price_micros, result.hold) == (6_000, "none")
    assert set(await usage_of(database, call)) == {"call_connected_seconds"}


async def test_non_terminal_call_is_not_finalized(database: Database) -> None:
    org_id = await funded_org(database)
    call = await ended_call(database, org_id, status="in_progress", connected="3")

    with pytest.raises(CallNotEndedError) as caught:
        await finalize(database, call)

    assert caught.value.status == "in_progress"
    assert await usage_of(database, call) == {}


async def test_unknown_call_and_org(database: Database) -> None:
    org_id = await funded_org(database)
    async with database.tenant_session(org_id) as session:
        with pytest.raises(AppError) as caught:
            await finalize_call(session, org_id, new_id(), now=datetime.now(UTC))
    assert caught.value.code == "CALL_NOT_FOUND"

    missing = new_id()
    async with database.tenant_session(missing) as session:
        with pytest.raises(AppError) as caught:
            await finalize_call(session, missing, new_id(), now=datetime.now(UTC))
    assert caught.value.code == "NOT_FOUND"


async def test_missing_snapshot_uses_current_org_pricing(database: Database) -> None:
    org_id = await funded_org(database, limits={"connected_rate_micros_per_min": 180_000})
    call = await ended_call(database, org_id, connected="10", pricing={})

    result = await finalize(database, call)

    assert result.price_micros == 30_000  # 10 s at the override rate of $0.18/min
    assert (await usage_of(database, call))["call_connected_seconds"][1] == 3_000


async def test_missing_plan_without_snapshot_is_unavailable(database: Database) -> None:
    org_id = await new_billing_org(database, plan_id=f"gone-{secrets.token_hex(4)}")
    call = await ended_call(database, org_id, connected="10", pricing={}, hold=False)

    with pytest.raises(AppError) as caught:
        await finalize(database, call)

    assert caught.value.code == "SERVICE_UNAVAILABLE"
