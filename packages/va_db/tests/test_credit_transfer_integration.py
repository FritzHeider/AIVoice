"""``CreditTransferRepo`` and ``use_org`` on the RLS-enforced ``va_app`` role (specs/04 §8.4; specs/03 §2)."""

from __future__ import annotations

import uuid

import pytest
from identity_db import IdentityDb
from model_factories import make_org
from sqlalchemy import func, select, text

from va_common.errors import AppError
from va_common.ids import new_id
from va_db.models import CreditHold, CreditHoldStatus, CreditLedger, Org
from va_db.repositories import (
    INSUFFICIENT_TRANSFER_MESSAGE,
    SUB_ACCOUNT_REFERENCE,
    CreditTransferRepo,
)
from va_db.session import use_org

pytestmark = pytest.mark.integration

DOLLAR = 1_000_000


async def org_pair(
    identity_db: IdentityDb, balance: int = 50 * DOLLAR, **parent: object
) -> tuple[uuid.UUID, uuid.UUID]:
    """A parent with ``balance`` micros (one ``grant`` ledger row) and an empty sub-account."""
    async with identity_db.database.system_session() as session:
        parent_org = await make_org(session, credits_balance_micros=balance, **parent)
        child = await make_org(session, parent_org_id=parent_org.id)
        session.add(
            CreditLedger(
                org_id=parent_org.id,
                entry_type="grant",
                amount_micros=balance,
                balance_after_micros=balance,
                idempotency_key=f"grant:{new_id()}",
            )
        )
        return parent_org.id, child.id


async def add_hold(identity_db: IdentityDb, org_id: uuid.UUID, micros: int, status: CreditHoldStatus) -> None:
    async with identity_db.database.system_session() as session:
        expires = await session.scalar(select(func.now() + text("interval '1 hour'")))
        session.add(
            CreditHold(
                org_id=org_id,
                reference_type="call",
                reference_id=str(new_id()),
                amount_micros=micros,
                status=status,
                expires_at=expires,
            )
        )


async def balances(identity_db: IdentityDb, *org_ids: uuid.UUID) -> list[tuple[int, int]]:
    """Per org: (``credits_balance_micros``, sum of its ledger rows)."""
    async with identity_db.database.system_session() as session:
        result = []
        for org_id in org_ids:
            org = await session.get(Org, org_id)
            assert org is not None
            ledger = await session.scalar(
                select(func.coalesce(func.sum(CreditLedger.amount_micros), 0)).where(
                    CreditLedger.org_id == org_id
                )
            )
            result.append((org.credits_balance_micros, int(ledger or 0)))
        return result


async def transfer(
    identity_db: IdentityDb,
    pair: tuple[uuid.UUID, uuid.UUID],
    micros: int,
    key: str,
    *,
    session_org: uuid.UUID,
) -> None:
    sender, receiver = pair
    async with identity_db.database.tenant_session(session_org) as session:
        await CreditTransferRepo(session).transfer(sender, receiver, micros, idempotency_key=key)


async def test_transfer_writes_both_rows_and_balances(identity_db: IdentityDb) -> None:
    parent_id, child_id = await org_pair(identity_db)
    async with identity_db.database.tenant_session(child_id) as session:
        moved = await CreditTransferRepo(session).transfer(
            parent_id, child_id, 10_500_000, idempotency_key="xfer:one", description="Initial credits"
        )
        assert await session.scalar(text("SELECT current_setting('app.org_id', true)")) == str(child_id)
    out_entry, in_entry = moved.out_entry, moved.in_entry
    assert (out_entry.org_id, out_entry.entry_type, out_entry.amount_micros) == (
        parent_id,
        "transfer_out",
        -10_500_000,
    )
    assert (in_entry.org_id, in_entry.entry_type, in_entry.amount_micros) == (
        child_id,
        "transfer_in",
        10_500_000,
    )
    assert (out_entry.balance_after_micros, in_entry.balance_after_micros) == (39_500_000, 10_500_000)
    assert (out_entry.reference_type, out_entry.reference_id) == (SUB_ACCOUNT_REFERENCE, str(child_id))
    assert (in_entry.reference_type, in_entry.reference_id) == (SUB_ACCOUNT_REFERENCE, str(parent_id))
    assert out_entry.idempotency_key == in_entry.idempotency_key == "xfer:one"
    assert in_entry.description == "Initial credits"
    assert out_entry.created_at is not None
    assert await balances(identity_db, parent_id, child_id) == [
        (39_500_000, 39_500_000),
        (10_500_000, 10_500_000),
    ]


async def test_transfer_from_a_session_of_the_sender(identity_db: IdentityDb) -> None:
    parent_id, child_id = await org_pair(identity_db, balance=5 * DOLLAR)
    await transfer(identity_db, (parent_id, child_id), 5 * DOLLAR, "xfer:all", session_org=parent_id)
    assert await balances(identity_db, parent_id, child_id) == [(0, 0), (5 * DOLLAR, 5 * DOLLAR)]


async def test_insufficient_credits_writes_nothing(identity_db: IdentityDb) -> None:
    parent_id, child_id = await org_pair(identity_db, balance=5 * DOLLAR, credit_limit_micros=100 * DOLLAR)
    with pytest.raises(AppError) as info:
        await transfer(
            identity_db, (parent_id, child_id), 5 * DOLLAR + 1, "xfer:too-much", session_org=child_id
        )
    assert (info.value.code, info.value.message) == ("INSUFFICIENT_CREDITS", INSUFFICIENT_TRANSFER_MESSAGE)
    assert await balances(identity_db, parent_id, child_id) == [(5 * DOLLAR, 5 * DOLLAR), (0, 0)]


async def test_only_held_holds_reduce_the_available_balance(identity_db: IdentityDb) -> None:
    parent_id, child_id = await org_pair(identity_db, balance=10 * DOLLAR)
    await add_hold(identity_db, parent_id, 4 * DOLLAR, "held")
    await add_hold(identity_db, parent_id, 3 * DOLLAR, "captured")
    await add_hold(identity_db, parent_id, 3 * DOLLAR, "released")
    with pytest.raises(AppError) as info:
        await transfer(
            identity_db, (parent_id, child_id), 6 * DOLLAR + 1, "xfer:over-held", session_org=child_id
        )
    assert info.value.code == "INSUFFICIENT_CREDITS"
    await transfer(identity_db, (parent_id, child_id), 6 * DOLLAR, "xfer:available", session_org=child_id)
    assert await balances(identity_db, parent_id, child_id) == [
        (4 * DOLLAR, 4 * DOLLAR),
        (6 * DOLLAR, 6 * DOLLAR),
    ]


async def test_duplicate_idempotency_key_is_a_conflict(identity_db: IdentityDb) -> None:
    parent_id, child_id = await org_pair(identity_db)
    await transfer(identity_db, (parent_id, child_id), DOLLAR, "xfer:same", session_org=child_id)
    with pytest.raises(AppError) as info:
        await transfer(identity_db, (parent_id, child_id), DOLLAR, "xfer:same", session_org=child_id)
    assert info.value.code == "CONFLICT"
    assert await balances(identity_db, parent_id, child_id) == [(49 * DOLLAR, 49 * DOLLAR), (DOLLAR, DOLLAR)]


@pytest.mark.parametrize("micros", [0, -DOLLAR])
async def test_non_positive_amount_is_a_programming_error(identity_db: IdentityDb, micros: int) -> None:
    parent_id, child_id = await org_pair(identity_db)
    with pytest.raises(ValueError, match="amount_micros"):
        await transfer(identity_db, (parent_id, child_id), micros, "xfer:zero", session_org=child_id)


async def test_same_org_and_unknown_org(identity_db: IdentityDb) -> None:
    parent_id, _ = await org_pair(identity_db)
    with pytest.raises(ValueError, match="two different orgs"):
        await transfer(identity_db, (parent_id, parent_id), DOLLAR, "xfer:self", session_org=parent_id)
    with pytest.raises(AppError) as info:
        await transfer(identity_db, (parent_id, new_id()), DOLLAR, "xfer:nobody", session_org=parent_id)
    assert info.value.code == "NOT_FOUND"


async def test_sequential_transfers_cannot_overdraw(identity_db: IdentityDb) -> None:
    parent_id, child_id = await org_pair(identity_db, balance=10 * DOLLAR)
    await transfer(identity_db, (parent_id, child_id), 7 * DOLLAR, "xfer:first", session_org=child_id)
    with pytest.raises(AppError) as info:
        await transfer(identity_db, (parent_id, child_id), 7 * DOLLAR, "xfer:second", session_org=child_id)
    assert info.value.code == "INSUFFICIENT_CREDITS"
    assert await balances(identity_db, parent_id, child_id) == [
        (3 * DOLLAR, 3 * DOLLAR),
        (7 * DOLLAR, 7 * DOLLAR),
    ]


async def test_use_org_switches_and_restores_the_rls_org(identity_db: IdentityDb) -> None:
    parent_id, child_id = await org_pair(identity_db)
    current = text("SELECT current_setting('app.org_id', true)")
    visible = select(func.count()).select_from(CreditLedger)
    async with identity_db.database.tenant_session(child_id) as session:
        assert await session.scalar(visible) == 0
        async with use_org(session, parent_id):
            assert await session.scalar(current) == str(parent_id)
            assert await session.scalar(visible) == 1
        assert await session.scalar(current) == str(child_id)
        with pytest.raises(RuntimeError, match="boom"):
            async with use_org(session, parent_id):
                raise RuntimeError("boom")
        assert await session.scalar(current) == str(child_id)
    async with identity_db.database.global_session() as session:
        async with use_org(session, parent_id):
            assert await session.scalar(visible) == 1
        assert await session.scalar(current) in {None, ""}
        assert await session.scalar(visible) == 0
