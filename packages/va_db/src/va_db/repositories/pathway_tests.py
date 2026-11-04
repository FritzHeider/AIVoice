"""Tenant data access for pathway unit tests and test runs (specs/12-pathways-api.md §8; specs/03 §5).

Rules: specs/12 §8 Decision P07-T11b. A test or run belongs to one pathway: reading it under another pathway
of the same org is ``NOT_FOUND`` like another org's row. Lists are newest first (``created_at DESC, id
DESC``) with the cursor format of :mod:`va_db.pagination`. Unit tests have no ``deleted_at``: a delete removes
the row.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from va_common.time import Clock
from va_db.models import PathwayTestRun, PathwayTestRunKind, PathwayUnitTest
from va_db.pagination import DEFAULT_PAGE_LIMIT, Page
from va_db.repositories.base import NotFoundError, TenantRepository
from va_domain.pathway_tests import FinishedRunStatus, run_status

__all__ = ["RUN_RESOURCE", "TEST_RESOURCE", "PathwayRunRepo", "PathwayTestRepo"]

_LIVE_RUN: Final = ("queued", "running")

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

    async def ordered_ids(self, pathway_id: uuid.UUID, *, limit: int) -> list[uuid.UUID]:
        """At most ``limit`` ids of the pathway's tests, oldest first (``created_at, id``)."""
        rows = await self.session.scalars(
            select(PathwayUnitTest.id)
            .where(PathwayUnitTest.org_id == self.org_id, PathwayUnitTest.pathway_id == pathway_id)
            .order_by(PathwayUnitTest.created_at, PathwayUnitTest.id)
            .limit(limit)
        )
        return list(rows)

    async def find(self, pathway_id: uuid.UUID, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, PathwayUnitTest]:
        """The tests of the pathway among ``ids`` (others are left out), by id."""
        if not ids:
            return {}
        rows = await self.session.scalars(
            select(PathwayUnitTest).where(
                PathwayUnitTest.org_id == self.org_id,
                PathwayUnitTest.pathway_id == pathway_id,
                PathwayUnitTest.id.in_(list(ids)),
            )
        )
        return {row.id: row for row in rows}

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

    async def add(
        self, pathway_id: uuid.UUID, *, version_number: int, kind: PathwayTestRunKind
    ) -> PathwayTestRun:
        """Insert a ``queued`` run of the pathway version with no results."""
        return await self._rows.create(
            {
                "pathway_id": pathway_id,
                "version_number": version_number,
                "kind": kind,
                "status": "queued",
                "results": [],
            }
        )

    async def lock(self, run_id: uuid.UUID) -> PathwayTestRun | None:
        """The run row locked ``FOR UPDATE`` (``None`` when absent)."""
        found = await self.session.scalars(
            select(PathwayTestRun)
            .where(PathwayTestRun.org_id == self.org_id, PathwayTestRun.id == run_id)
            .with_for_update()
        )
        return found.one_or_none()

    async def begin(self, run_id: uuid.UUID, now: datetime) -> bool:
        """``queued`` → ``running`` (``started_at`` = ``now``); true while the run is queued or running."""
        run = await self.lock(run_id)
        if run is None or run.status not in _LIVE_RUN:
            return False
        if run.status == "queued":
            run.status = "running"
            run.started_at = now
            await self.session.flush()
        return True

    async def put_result(self, run_id: uuid.UUID, item: Mapping[str, Any], *, replace: bool = True) -> bool:
        """Store one test's result item, replacing an earlier item of the same ``test_id`` (a retried test is
        never listed twice; with ``replace=False`` an earlier item is kept instead); false when the run is
        gone or already finished."""
        run = await self.lock(run_id)
        if run is None or run.status not in _LIVE_RUN:
            return False
        test_id = item.get("test_id")
        if not replace and any(
            isinstance(old, dict) and old.get("test_id") == test_id for old in run.results
        ):
            return True
        kept = [old for old in run.results if not (isinstance(old, dict) and old.get("test_id") == test_id)]
        # A new list object: SQLAlchemy does not track in-place changes of a JSONB value.
        run.results = [*kept, dict(item)]
        await self.session.flush()
        return True

    async def finish(
        self, run_id: uuid.UUID, order: Sequence[str], now: datetime
    ) -> FinishedRunStatus | None:
        """Order the results by ``order`` (test ids; others last), set the final status (a test of ``order``
        without a result counts as an error) and ``finished_at``; ``None`` when the run is gone or already
        finished."""
        run = await self.lock(run_id)
        if run is None or run.status not in _LIVE_RUN:
            return None
        rank = {test_id: index for index, test_id in enumerate(order)}
        items = [item for item in run.results if isinstance(item, dict)]
        results = sorted(items, key=lambda item: rank.get(str(item.get("test_id")), len(rank)))
        statuses: list[str | None] = [item.get("status") for item in results]
        reported = {item.get("test_id") for item in results}
        statuses.extend(None for test_id in order if test_id not in reported)
        status = run_status(statuses)
        run.results = results
        run.status = status
        run.started_at = run.started_at or now
        run.finished_at = now
        await self.session.flush()
        return status

    async def fail(self, run_id: uuid.UUID, now: datetime) -> bool:
        """Mark a run that could not run as ``error`` (``finished_at`` = ``now``); false when it is gone or
        already finished."""
        run = await self.lock(run_id)
        if run is None or run.status not in _LIVE_RUN:
            return False
        run.status = "error"
        run.finished_at = now
        await self.session.flush()
        return True
