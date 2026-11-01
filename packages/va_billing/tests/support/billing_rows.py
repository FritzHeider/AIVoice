"""Org and hold rows for the va_billing DB tests (each test uses fresh orgs)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from billing_helpers import NOW
from model_factories import make_org
from sqlalchemy import select

from va_db.models import CreditHold
from va_db.session import Database

__all__ = ["holds_of", "new_billing_org"]


async def new_billing_org(  # noqa: PLR0913 - keyword-only org columns
    database: Database,
    *,
    plan_id: str = "build",
    balance: int = 0,
    credit_limit: int = 0,
    created_at: datetime | None = None,
    settings: dict[str, Any] | None = None,
    limits: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Insert an org (created 30 days before ``NOW`` by default) through the system role; its id."""
    values: dict[str, Any] = {
        "plan_id": plan_id,
        "credits_balance_micros": balance,
        "credit_limit_micros": credit_limit,
        "settings": settings or {},
        "limits": limits or {},
    }
    values["created_at"] = created_at or NOW - timedelta(days=30)
    async with database.system_session() as session:
        org = await make_org(session, **values)
        return org.id


async def holds_of(database: Database, org_id: uuid.UUID) -> list[CreditHold]:
    """The org's holds, oldest first."""
    async with database.system_session() as session:
        rows = await session.scalars(
            select(CreditHold)
            .where(CreditHold.org_id == org_id)
            .order_by(CreditHold.created_at, CreditHold.id)
        )
        return list(rows.all())
