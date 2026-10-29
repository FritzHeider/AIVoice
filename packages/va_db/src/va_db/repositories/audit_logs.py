"""Reading one org's audit log (specs/04-auth-tenancy-rbac.md §13 ``GET /v1/audit_logs``).

Rows are written by :func:`va_db.audit.record`; :class:`AuditLogRepo` pages them newest first on a
``tenant_session`` (RLS plus the explicit ``org_id`` filter of :class:`TenantRepository`). Every filter is an
exact match; ``since`` is inclusive and ``until`` exclusive.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement
from sqlalchemy.ext.asyncio import AsyncSession

from va_db.models import AuditLog
from va_db.pagination import DEFAULT_PAGE_LIMIT, Page
from va_db.repositories.base import TenantRepository

__all__ = ["AuditLogFilter", "AuditLogRepo"]


@dataclass(frozen=True, slots=True)
class AuditLogFilter:
    """Optional exact-match filters and a ``[since, until)`` time window (timezone-aware datetimes)."""

    action: str | None = None
    actor_id: uuid.UUID | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    since: datetime | None = None
    until: datetime | None = None

    def conditions(self) -> list[ColumnElement[bool]]:
        """The SQL conditions of the filters that are set."""
        conditions: list[ColumnElement[bool]] = []
        if self.action is not None:
            conditions.append(AuditLog.action == self.action)
        if self.actor_id is not None:
            conditions.append(AuditLog.actor_id == self.actor_id)
        if self.resource_type is not None:
            conditions.append(AuditLog.resource_type == self.resource_type)
        if self.resource_id is not None:
            conditions.append(AuditLog.resource_id == self.resource_id)
        if self.since is not None:
            conditions.append(AuditLog.created_at >= self.since)
        if self.until is not None:
            conditions.append(AuditLog.created_at < self.until)
        return conditions


class AuditLogRepo(TenantRepository[AuditLog]):
    """One org's ``audit_logs`` rows."""

    def __init__(self, session: AsyncSession, org_id: uuid.UUID) -> None:
        super().__init__(AuditLog, session, org_id)

    async def page(
        self, filters: AuditLogFilter, *, limit: int = DEFAULT_PAGE_LIMIT, cursor: str | None = None
    ) -> Page[AuditLog]:
        """One page of matching rows, newest first, after ``cursor`` when given."""
        return await self.list(limit=limit, cursor=cursor, where=filters.conditions())
