"""``PathwayTestRepo`` and ``PathwayRunRepo`` on a migrated database: pathway scoping, pages, kind filter,
hard delete, and org isolation (specs/12 §8; Decision P07-T11b)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from pathway_rows import GRAPH
from tenant_rows import OrgFactory, open_database

from va_db.models import PathwayTestRun, PathwayTestRunKind
from va_db.repositories import NotFoundError, PathwayRepo, PathwayRunRepo, PathwayTestRepo
from va_db.session import Database

pytestmark = pytest.mark.integration


@pytest.fixture
async def database(migrated_url: str) -> AsyncIterator[Database]:
    async with open_database(migrated_url) as db:
        yield db


async def pathway(database: Database, org_id: uuid.UUID) -> uuid.UUID:
    async with database.tenant_session(org_id) as session:
        row, _ = await PathwayRepo(session, org_id).create(
            name="Orders", description="", folder_id=None, graph=GRAPH
        )
        return row.id


async def unit_test(database: Database, org_id: uuid.UUID, pathway_id: uuid.UUID, name: str) -> uuid.UUID:
    values = {"name": name, "node_id": "start", "history": [], "user_message": "Hi", "expected": {}}
    async with database.tenant_session(org_id) as session:
        return (await PathwayTestRepo(session, org_id).add(pathway_id, values)).id


async def run(
    database: Database, org_id: uuid.UUID, pathway_id: uuid.UUID, kind: PathwayTestRunKind
) -> uuid.UUID:
    row = PathwayTestRun(
        org_id=org_id, pathway_id=pathway_id, version_number=1, kind=kind, status="queued", results=[]
    )
    async with database.tenant_session(org_id) as session:
        session.add(row)
        await session.flush()
        return row.id


async def test_tests_are_scoped_to_their_pathway_and_org(database: Database) -> None:
    org_id, other = await OrgFactory(database).create(), await OrgFactory(database).create()
    orders, support = await pathway(database, org_id), await pathway(database, org_id)
    first = await unit_test(database, org_id, orders, "First")
    second = await unit_test(database, org_id, orders, "Second")
    await unit_test(database, org_id, support, "Elsewhere")
    foreign = await unit_test(database, other, await pathway(database, other), "Foreign")
    async with database.tenant_session(org_id) as session:
        repo = PathwayTestRepo(session, org_id)
        page = await repo.page(orders, limit=1)
        assert [row.id for row in page.items] == [second]
        rest = await repo.page(orders, limit=1, cursor=page.next_cursor)
        assert ([row.id for row in rest.items], rest.next_cursor) == ([first], None)
        row = await repo.get(orders, first)
        await repo.change(row, {"name": "Renamed", "simulated_variations": 0})
        assert (row.name, row.simulated_variations) == ("Renamed", 0)
        for pathway_id, test_id in ((support, first), (orders, foreign), (orders, uuid.uuid4())):
            with pytest.raises(NotFoundError, match="Pathway test not found"):
                await repo.get(pathway_id, test_id)
        await repo.delete(row)
    async with database.tenant_session(org_id) as session:
        with pytest.raises(NotFoundError):
            await PathwayTestRepo(session, org_id).get(orders, first)


async def test_runs_filter_by_kind_and_pathway(database: Database) -> None:
    org_id, other = await OrgFactory(database).create(), await OrgFactory(database).create()
    orders, support = await pathway(database, org_id), await pathway(database, org_id)
    unit = await run(database, org_id, orders, "unit")
    regression = await run(database, org_id, orders, "regression")
    elsewhere = await run(database, org_id, support, "unit")
    foreign = await run(database, other, await pathway(database, other), "unit")
    async with database.tenant_session(org_id) as session:
        repo = PathwayRunRepo(session, org_id)
        assert [row.id for row in (await repo.page(orders)).items] == [regression, unit]
        assert [row.id for row in (await repo.page(orders, kind="unit")).items] == [unit]
        assert (await repo.page(orders, kind="simulation")).items == ()
        assert (await repo.get(orders, unit)).kind == "unit"
        for test_id in (elsewhere, foreign):
            with pytest.raises(NotFoundError, match="Pathway test run not found"):
                await repo.get(orders, test_id)
