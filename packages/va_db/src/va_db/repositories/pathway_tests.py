"""Tenant data access for pathway unit tests and test runs (specs/12-pathways-api.md §8; specs/03 §5).

Rules: specs/12 §8 Decision P07-T11b. A test or run belongs to one pathway: reading it under another pathway
of the same org is ``NOT_FOUND`` like another org's row. Lists are newest first (``created_at DESC, id
DESC``) with the cursor format of :mod:`va_db.pagination`. Unit tests have no ``deleted_at``: a delete removes
the row.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from va_common.time import Clock
from va_db.models import PathwayTestRun, PathwayTestRunKind, PathwayUnitTest
from va_db.pagination import DEFAULT_PAGE_LIMIT, Page
from va_db.repositories.base import NotFoundError, TenantRepository

__all__ = ["RUN_RESOURCE", "TEST_RESOURCE", "PathwayRunRepo", "PathwayTestRepo"]

TEST_RESOURCE: Final = "Pathway test"
"""The resource name of the unit test ``NOT_FOUND`` message ("Pathway test not found.")."""
RUN_RESOURCE: Final = "Pathway test run"
"""The resource name of the run ``NOT_FOUND`` message ("Pathway test run not found.")."""


class PathwayTestRepo:
    """The org's pathway unit tests; use inside ``tenant_session(org_id)`` with the same org."""

    def __init__(self, session: AsyncSession, org_id: uuid.UUID, *, clock: Clock | None = None) -> None:
        self.session = session
        self.org_id = org_id
        self._rows = TenantRepository(PathwayUnitTest, session, org_id, clock=clock)

    async def page(
        self, pathway_id: uuid.UUID, *, limit: int = DEFAULT_PAGE_LIMIT, cursor: str | None = None
    ) -> Page[PathwayUnitTest]:
        """One page of the pathway's tests, newest first."""
        return await self._rows.list(
            limit=limit, cursor=cursor, where=[PathwayUnitTest.pathway_id == pathway_id]
        )

    async def get(self, pathway_id: uuid.UUID, test_id: uuid.UUID) -> PathwayUnitTest:
        """The test ``test_id`` of the pathway; ``NOT_FOUND`` "Pathway test not found." otherwise."""
        try:
            test = await self._rows.get(test_id)
        except NotFoundError:
            raise NotFoundError(TEST_RESOURCE) from None
        if test.pathway_id != pathway_id:
            raise NotFoundError(TEST_RESOURCE)
        return test

    async def add(self, pathway_id: uuid.UUID, values: Mapping[str, Any]) -> PathwayUnitTest:
        """Insert a test of the pathway (``values`` = the other columns)."""
        return await self._rows.create({**values, "pathway_id": pathway_id})

    async def change(self, test: PathwayUnitTest, values: Mapping[str, Any]) -> PathwayUnitTest:
        """Set columns of ``test`` (``updated_at`` is set by the trigger)."""
        for key, value in values.items():
            setattr(test, key, value)
        await self.session.flush()
        await self.session.refresh(test)
        return test

    async def delete(self, test: PathwayUnitTest) -> None:
        """Remove ``test``."""
        await self.session.delete(test)
        await self.session.flush()


class PathwayRunRepo:
    """The org's pathway test runs (unit, simulation, regression); use inside ``tenant_session(org_id)``."""

    def __init__(self, session: AsyncSession, org_id: uuid.UUID, *, clock: Clock | None = None) -> None:
        self.session = session
        self.org_id = org_id
        self._rows = TenantRepository(PathwayTestRun, session, org_id, clock=clock)

    async def page(
        self,
        pathway_id: uuid.UUID,
        *,
        kind: PathwayTestRunKind | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        cursor: str | None = None,
    ) -> Page[PathwayTestRun]:
        """One page of the pathway's runs (of one ``kind`` when given), newest first."""
        where = [PathwayTestRun.pathway_id == pathway_id]
        if kind is not None:
            where.append(PathwayTestRun.kind == kind)
        return await self._rows.list(limit=limit, cursor=cursor, where=where)

    async def get(self, pathway_id: uuid.UUID, run_id: uuid.UUID) -> PathwayTestRun:
        """The run ``run_id`` of the pathway; ``NOT_FOUND`` "Pathway test run not found." otherwise."""
        try:
            run = await self._rows.get(run_id)
        except NotFoundError:
            raise NotFoundError(RUN_RESOURCE) from None
        if run.pathway_id != pathway_id:
            raise NotFoundError(RUN_RESOURCE)
        return run
