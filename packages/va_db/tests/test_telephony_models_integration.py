"""Telephony models against Postgres, on the rolled-back ``scratch_session`` schema."""

from __future__ import annotations

import itertools
import os
import secrets
import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest
from model_factories import ModelBuilder, assert_enum_check_enforced, flushed, make_org
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from va_db.base import Base
from va_db.models import (
    NumberOrder,
    NumberOrderStatus,
    NumberPool,
    NumberPoolMember,
    NumberPoolStrategy,
    NumberType,
    PhoneNumber,
    PhoneNumberStatus,
    TelnyxAccount,
    TelnyxAccountStatus,
    TelnyxEvent,
)

pytestmark = pytest.mark.integration

_E164 = "+15555550100"
# Distinct fictional numbers for builders (duplicates among pending/active rows violate the partial index).
_FRESH_NUMBERS = itertools.count(200)


def _number(org_id: uuid.UUID, e164: str = _E164, **values: Any) -> PhoneNumber:
    return PhoneNumber(org_id=org_id, e164=e164, country_code="US", **values)


async def _telnyx_account(session: AsyncSession) -> TelnyxAccount:
    org = await make_org(session)
    return TelnyxAccount(
        org_id=org.id,
        label="BYO",
        api_key_enc=os.urandom(48),
        public_key=secrets.token_hex(32),
        encrypted_key_handle=secrets.token_hex(16),
    )


async def _build_telnyx_account(session: AsyncSession) -> Base:
    return await _telnyx_account(session)


async def _build_phone_number(session: AsyncSession) -> Base:
    org = await make_org(session)
    return _number(org.id, f"+1555555{next(_FRESH_NUMBERS):04d}")


async def _build_number_order(session: AsyncSession) -> Base:
    org = await make_org(session)
    return NumberOrder(org_id=org.id, requested_numbers=[_E164], status="pending")


async def _build_number_pool(session: AsyncSession) -> Base:
    org = await make_org(session)
    return NumberPool(org_id=org.id, name=f"pool-{secrets.token_hex(4)}")


async def _count(session: AsyncSession, model: type[NumberPoolMember], **where: Any) -> int:
    statement = select(func.count()).select_from(model).filter_by(**where)
    result: int = (await session.execute(statement)).scalar_one()
    return result


async def test_phone_number_defaults(scratch_session: AsyncSession) -> None:
    org = await make_org(scratch_session)
    number = await flushed(scratch_session, _number(org.id))
    assert number.capabilities == ["voice"]
    assert (number.inbound_config, number.sms_config) == ({}, {})
    assert (number.monthly_cost_micros, number.number_type, number.status) == (0, "local", "active")
    assert (number.telnyx_account_id, number.deleted_at, number.released_at) == (None, None, None)

    account = await flushed(scratch_session, await _telnyx_account(scratch_session))
    assert account.status == "pending"
    pool = await flushed(scratch_session, NumberPool(org_id=org.id, name="outbound"))
    assert (pool.strategy, pool.max_calls_per_number_per_day) == ("round_robin", 100)
    member = await flushed(
        scratch_session, NumberPoolMember(pool_id=pool.id, phone_number_id=number.id, org_id=org.id)
    )
    assert (member.calls_today, member.calls_today_date, member.last_used_at) == (0, None, None)


async def test_e164_unique_among_active_numbers(scratch_session: AsyncSession) -> None:
    org_a, org_b = await make_org(scratch_session), await make_org(scratch_session)
    held = await flushed(scratch_session, _number(org_a.id))
    for status in ("pending", "active"):
        with pytest.raises(IntegrityError, match="phone_numbers_e164_active_uq"):
            async with scratch_session.begin_nested():
                await flushed(scratch_session, _number(org_b.id, status=status))

    await scratch_session.execute(
        update(PhoneNumber).where(PhoneNumber.id == held.id).values(status="released")
    )
    await flushed(scratch_session, _number(org_b.id, status="pending"))
    await flushed(scratch_session, _number(org_a.id, status="released"))
    await flushed(scratch_session, _number(org_a.id, status="releasing"))
    await flushed(scratch_session, _number(org_b.id, status="releasing"))
    statuses = (
        await scratch_session.execute(select(PhoneNumber.status).where(PhoneNumber.e164 == _E164))
    ).scalars()
    assert sorted(statuses) == ["pending", "released", "released", "releasing", "releasing"]


@pytest.mark.parametrize(
    ("builder", "column", "literal"),
    [
        (_build_telnyx_account, "status", TelnyxAccountStatus),
        (_build_phone_number, "number_type", NumberType),
        (_build_phone_number, "status", PhoneNumberStatus),
        (_build_number_order, "status", NumberOrderStatus),
        (_build_number_pool, "strategy", NumberPoolStrategy),
    ],
)
async def test_enum_values_accepted_and_rejected(
    scratch_session: AsyncSession, builder: ModelBuilder, column: str, literal: Any
) -> None:
    await assert_enum_check_enforced(scratch_session, builder, column, literal)


async def test_number_pool_name_unique_per_org(scratch_session: AsyncSession) -> None:
    org, other = await make_org(scratch_session), await make_org(scratch_session)
    await flushed(scratch_session, NumberPool(org_id=org.id, name="outbound"))
    with pytest.raises(IntegrityError, match="number_pools_org_id_name_key"):
        async with scratch_session.begin_nested():
            await flushed(scratch_session, NumberPool(org_id=org.id, name="outbound"))
    await flushed(scratch_session, NumberPool(org_id=other.id, name="outbound"))


async def test_pool_member_composite_pk_and_cascades(scratch_session: AsyncSession) -> None:
    org = await make_org(scratch_session)
    pool = await flushed(scratch_session, NumberPool(org_id=org.id, name="outbound"))
    first = await flushed(scratch_session, _number(org.id))
    second = await flushed(scratch_session, _number(org.id, "+15555550101"))
    for number in (first, second):
        await flushed(
            scratch_session, NumberPoolMember(pool_id=pool.id, phone_number_id=number.id, org_id=org.id)
        )
    with pytest.raises(IntegrityError, match="number_pool_members_pkey"):
        async with scratch_session.begin_nested():
            await flushed(
                scratch_session, NumberPoolMember(pool_id=pool.id, phone_number_id=first.id, org_id=org.id)
            )
    scratch_session.expunge_all()

    await scratch_session.execute(delete(PhoneNumber).where(PhoneNumber.id == first.id))
    assert await _count(scratch_session, NumberPoolMember, pool_id=pool.id) == 1
    await scratch_session.execute(delete(NumberPool).where(NumberPool.id == pool.id))
    assert await _count(scratch_session, NumberPoolMember, phone_number_id=second.id) == 0


async def test_telnyx_account_delete_sets_number_account_null(scratch_session: AsyncSession) -> None:
    account = await flushed(scratch_session, await _telnyx_account(scratch_session))
    number = await flushed(scratch_session, _number(account.org_id, telnyx_account_id=account.id))
    with pytest.raises(IntegrityError, match="telnyx_accounts_encrypted_key_handle_key"):
        async with scratch_session.begin_nested():
            duplicate = await _telnyx_account(scratch_session)
            duplicate.encrypted_key_handle = account.encrypted_key_handle
            await flushed(scratch_session, duplicate)
    scratch_session.expunge_all()

    await scratch_session.execute(delete(TelnyxAccount).where(TelnyxAccount.id == account.id))
    linked = select(PhoneNumber.telnyx_account_id).where(PhoneNumber.id == number.id)
    assert (await scratch_session.execute(linked)).scalar_one() is None


async def test_telnyx_event_without_org_or_call(scratch_session: AsyncSession) -> None:
    payload = {"data": {"event_type": "call.initiated", "payload": {"to": _E164, "nested": [1, None, "x"]}}}
    event = await flushed(
        scratch_session,
        TelnyxEvent(
            telnyx_event_id=str(uuid.uuid4()),
            event_type="call.initiated",
            call_id=uuid.uuid4(),
            occurred_at=datetime(2026, 10, 27, 12, 0, tzinfo=UTC),
            payload=payload,
        ),
    )
    assert event.org_id is None
    assert event.payload == payload
    assert event.occurred_at == datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
    assert event.processed_at is None
    with pytest.raises(IntegrityError, match="telnyx_events_telnyx_event_id_key"):
        async with scratch_session.begin_nested():
            await flushed(
                scratch_session,
                TelnyxEvent(
                    telnyx_event_id=event.telnyx_event_id,
                    event_type="call.answered",
                    occurred_at=event.occurred_at,
                    payload={},
                ),
            )


async def test_pool_member_daily_counters_round_trip(scratch_session: AsyncSession) -> None:
    org = await make_org(scratch_session)
    pool = await flushed(scratch_session, NumberPool(org_id=org.id, name="outbound"))
    number = await flushed(scratch_session, _number(org.id))
    member = await flushed(
        scratch_session,
        NumberPoolMember(
            pool_id=pool.id,
            phone_number_id=number.id,
            org_id=org.id,
            calls_today=7,
            calls_today_date=date(2026, 10, 27),
            last_used_at=datetime(2026, 10, 27, 9, 30, tzinfo=UTC),
        ),
    )
    assert member.calls_today_date == date(2026, 10, 27)
    assert member.calls_today == 7
