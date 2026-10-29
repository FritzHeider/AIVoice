"""Credit transfers between a parent org and its sub-account (specs/04 §8.4; specs/24 §9).

A transfer runs inside the caller's ``va_app`` transaction (a ``tenant_session`` of either org). Both org rows
are locked ``FOR UPDATE`` in id order, so two transfers touching the same pair never deadlock and a concurrent
debit of the sender waits for this one. The sender may give away its balance minus its open holds; its
``credit_limit_micros`` is borrowed credit and cannot be transferred. Each ``credit_ledger`` row is written
with ``app.org_id`` switched to its own org (:func:`va_db.session.use_org`), so row-level security checks both
rows and no ``va_system`` session is needed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from va_common.errors import AppError
from va_common.ids import new_id
from va_db.models import CreditEntryType, CreditHold, CreditLedger, Org
from va_db.repositories.base import conflict_savepoint, require_positive
from va_db.session import use_org

__all__ = [
    "DUPLICATE_TRANSFER_MESSAGE",
    "INSUFFICIENT_TRANSFER_MESSAGE",
    "SUB_ACCOUNT_REFERENCE",
    "CreditTransfer",
    "CreditTransferRepo",
]

INSUFFICIENT_TRANSFER_MESSAGE: Final = "Insufficient credits for this transfer."
DUPLICATE_TRANSFER_MESSAGE: Final = "This credit transfer was already recorded."
SUB_ACCOUNT_REFERENCE: Final = "sub_account"


@dataclass(frozen=True, slots=True)
class CreditTransfer:
    """The two ledger rows of one transfer: the sender's ``transfer_out``, the receiver's ``transfer_in``."""

    out_entry: CreditLedger
    in_entry: CreditLedger


class CreditTransferRepo:
    """Moves credit between two orgs in the caller's transaction (``va_app``; RLS checks every ledger row)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def transfer(
        self,
        from_org_id: uuid.UUID,
        to_org_id: uuid.UUID,
        amount_micros: int,
        *,
        idempotency_key: str,
        description: str = "",
    ) -> CreditTransfer:
        """Move ``amount_micros`` from ``from_org_id`` to ``to_org_id`` and update both balances.

        ``INSUFFICIENT_CREDITS`` when the amount exceeds the sender's balance minus its ``held`` holds;
        ``CONFLICT`` when either org already has a ledger row with ``idempotency_key``; ``NOT_FOUND`` when an
        org does not exist. ``ValueError`` for a non-positive amount or a transfer to the same org.
        """
        require_positive("amount_micros", amount_micros)
        if from_org_id == to_org_id:
            msg = "a credit transfer needs two different orgs"
            raise ValueError(msg)
        sender, receiver = await self._lock(from_org_id, to_org_id)
        available = sender.credits_balance_micros - await self._held(from_org_id)
        if amount_micros > available:
            raise AppError("INSUFFICIENT_CREDITS", INSUFFICIENT_TRANSFER_MESSAGE)
        out_entry = await self._entry(
            sender,
            "transfer_out",
            -amount_micros,
            counterparty_id=to_org_id,
            idempotency_key=idempotency_key,
            description=description,
        )
        in_entry = await self._entry(
            receiver,
            "transfer_in",
            amount_micros,
            counterparty_id=from_org_id,
            idempotency_key=idempotency_key,
            description=description,
        )
        return CreditTransfer(out_entry=out_entry, in_entry=in_entry)

    async def _lock(self, from_org_id: uuid.UUID, to_org_id: uuid.UUID) -> tuple[Org, Org]:
        """Both org rows, locked ``FOR UPDATE`` in id order."""
        statement = (
            select(Org)
            .where(Org.id.in_((from_org_id, to_org_id)))
            .order_by(Org.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        orgs = {org.id: org for org in (await self.session.scalars(statement)).all()}
        if from_org_id not in orgs or to_org_id not in orgs:
            raise AppError("NOT_FOUND", "Organization not found.")
        return orgs[from_org_id], orgs[to_org_id]

    async def _held(self, org_id: uuid.UUID) -> int:
        """The sum of the org's ``held`` credit holds in micros."""
        statement = select(func.coalesce(func.sum(CreditHold.amount_micros), 0)).where(
            CreditHold.org_id == org_id, CreditHold.status == "held"
        )
        async with use_org(self.session, org_id):
            held = await self.session.scalar(statement)
        return int(held or 0)

    async def _entry(  # noqa: PLR0913 - one ledger row's attributes
        self,
        org: Org,
        entry_type: CreditEntryType,
        amount_micros: int,
        *,
        counterparty_id: uuid.UUID,
        idempotency_key: str,
        description: str,
    ) -> CreditLedger:
        """Apply ``amount_micros`` to the locked ``org`` and append its ledger row under its RLS context."""
        org.credits_balance_micros += amount_micros
        entry = CreditLedger(
            id=new_id(),
            org_id=org.id,
            entry_type=entry_type,
            amount_micros=amount_micros,
            balance_after_micros=org.credits_balance_micros,
            reference_type=SUB_ACCOUNT_REFERENCE,
            reference_id=str(counterparty_id),
            description=description,
            idempotency_key=idempotency_key,
        )
        async with use_org(self.session, org.id):
            async with conflict_savepoint(self.session, DUPLICATE_TRANSFER_MESSAGE):
                self.session.add(entry)
            await self.session.refresh(entry)
        return entry
