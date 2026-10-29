"""``grant_credits``: one ``grant`` ledger row per idempotency key, under the org row lock (va_db credits)."""

from __future__ import annotations

import pytest
from identity_db import IdentityDb
from model_factories import make_org
from sqlalchemy import select

from va_common.errors import AppError
from va_common.ids import new_id
from va_db.models import CreditLedger, Org
from va_db.repositories import MANUAL_REFERENCE, grant_credits

pytestmark = pytest.mark.integration

DOLLAR = 1_000_000


async def test_grant_writes_ledger_row_and_balance(identity_db: IdentityDb) -> None:
    async with identity_db.database.system_session() as session:
        org = await make_org(session, credits_balance_micros=2 * DOLLAR)
    async with identity_db.database.tenant_session(org.id) as session:
        granted = await grant_credits(
            session, org.id, 5 * DOLLAR, idempotency_key="seed:one", description="Starting credit"
        )
    assert granted is True
    async with identity_db.database.system_session() as session:
        stored = await session.get(Org, org.id)
        rows = (await session.scalars(select(CreditLedger).where(CreditLedger.org_id == org.id))).all()
    assert stored is not None
    assert stored.credits_balance_micros == 7 * DOLLAR
    assert [
        (row.entry_type, row.amount_micros, row.balance_after_micros, row.reference_type, row.description)
        for row in rows
    ] == [("grant", 5 * DOLLAR, 7 * DOLLAR, MANUAL_REFERENCE, "Starting credit")]


async def test_same_key_twice_grants_once(identity_db: IdentityDb) -> None:
    async with identity_db.database.system_session() as session:
        org = await make_org(session)
        assert await grant_credits(session, org.id, DOLLAR, idempotency_key="seed:twice") is True
    async with identity_db.database.system_session() as session:
        assert await grant_credits(session, org.id, DOLLAR, idempotency_key="seed:twice") is False
        assert await grant_credits(session, org.id, DOLLAR, idempotency_key="seed:other") is True
    async with identity_db.database.system_session() as session:
        stored = await session.get(Org, org.id)
        count = len((await session.scalars(select(CreditLedger).where(CreditLedger.org_id == org.id))).all())
    assert stored is not None
    assert (stored.credits_balance_micros, count) == (2 * DOLLAR, 2)


async def test_unknown_org_and_bad_amount(identity_db: IdentityDb) -> None:
    async with identity_db.database.system_session() as session:
        with pytest.raises(AppError) as caught:
            await grant_credits(session, new_id(), DOLLAR, idempotency_key="seed:none")
        assert caught.value.code == "NOT_FOUND"
        with pytest.raises(ValueError, match="amount_micros"):
            await grant_credits(session, new_id(), 0, idempotency_key="seed:zero")
