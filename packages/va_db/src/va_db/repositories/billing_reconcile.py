"""Data access for the hourly billing reconcile (specs/03-data-model.md §2; specs/24 §12).

The reconcile math (re-pricing ended calls, comparing usage) belongs to a later phase; this repository
reads ended calls, releases expired holds, and compares materialized balances with the ledger.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import BigInteger, func, select, update

from va_db.models import Call, CreditHold, CreditLedger, Org
from va_db.repositories.base import NotFoundError, SystemRepository, require_positive

__all__ = ["BalanceDrift", "BillingReconcileRepo"]


@dataclass(frozen=True, slots=True)
class BalanceDrift:
    """An org whose ``credits_balance_micros`` differs from the sum of its ledger entries."""

    org_id: uuid.UUID
    materialized_micros: int
    ledger_micros: int


class BillingReconcileRepo(SystemRepository):
    """Cross-org billing reads and repairs (system scope, specs/03 §2)."""

    async def calls_ended_between(
        self, start: datetime, end: datetime, *, after_id: uuid.UUID | None = None, limit: int = 1000
    ) -> list[Call]:
        """Calls with ``start <= ended_at < end`` by ascending id; pass the last id as ``after_id``."""
        statement = select(Call).where(Call.ended_at >= start, Call.ended_at < end)
        if after_id is not None:
            statement = statement.where(Call.id > after_id)
        statement = statement.order_by(Call.id).limit(require_positive("limit", limit))
        return list((await self.session.scalars(statement)).all())

    async def release_expired_holds(self, now: datetime, *, limit: int = 1000) -> list[CreditHold]:
        """Move up to ``limit`` ``held`` holds with ``expires_at <= now`` to ``released`` and return them."""
        expired = (
            select(CreditHold.id)
            .where(CreditHold.status == "held", CreditHold.expires_at <= now)
            .order_by(CreditHold.expires_at, CreditHold.id)
            .limit(require_positive("limit", limit))
            .with_for_update(skip_locked=True)
        )
        statement = (
            update(CreditHold)
            .where(CreditHold.id.in_(expired))
            .values(status="released")
            .returning(CreditHold)
            .execution_options(populate_existing=True, synchronize_session=False)
        )
        return list((await self.session.scalars(statement)).all())

    async def balance_drifts(self) -> list[BalanceDrift]:
        """Orgs whose materialized balance differs from their ledger sum (0 without entries), by org id."""
        ledger = (
            select(CreditLedger.org_id, func.sum(CreditLedger.amount_micros).cast(BigInteger).label("total"))
            .group_by(CreditLedger.org_id)
            .subquery()
        )
        total = func.coalesce(ledger.c.total, 0)
        statement = (
            select(Org.id, Org.credits_balance_micros, total)
            .outerjoin(ledger, ledger.c.org_id == Org.id)
            .where(total != Org.credits_balance_micros)
            .order_by(Org.id)
        )
        rows = (await self.session.execute(statement)).all()
        return [
            BalanceDrift(org_id=row[0], materialized_micros=row[1], ledger_micros=int(row[2])) for row in rows
        ]

    async def set_balance(self, org_id: uuid.UUID, balance_micros: int) -> None:
        """Overwrite an org's materialized ``credits_balance_micros``."""
        statement = (
            update(Org)
            .where(Org.id == org_id)
            .values(credits_balance_micros=balance_micros)
            .returning(Org.id)
        )
        if await self.session.scalar(statement) is None:
            raise NotFoundError(Org.__tablename__)
