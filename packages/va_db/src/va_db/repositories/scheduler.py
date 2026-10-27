"""Recovery sweep for scheduled calls (specs/03-data-model.md §2; specs/17)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from va_db.models import Call
from va_db.repositories.base import SystemRepository, require_positive

__all__ = ["SchedulerRepo"]


class SchedulerRepo(SystemRepository):
    """Finds scheduled calls whose start time has passed, across orgs (system scope, specs/03 §2)."""

    async def due_scheduled_calls(self, now: datetime, *, limit: int = 100) -> list[Call]:
        """Calls in status ``scheduled`` with ``start_time <= now``, earliest first."""
        statement = (
            select(Call)
            .where(Call.status == "scheduled", Call.start_time <= now)
            .order_by(Call.start_time, Call.id)
            .limit(require_positive("limit", limit))
        )
        return list((await self.session.scalars(statement)).all())
