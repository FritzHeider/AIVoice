"""``PathwayTestRepo`` and ``PathwayRunRepo`` on a migrated database: pathway scoping, pages, kind filter,
hard delete, and org isolation (specs/12 §8; Decision P07-T11b)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

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


@dataclass(frozen=True, slots=True)
class Seeded:
    """Two pathways of one org (tests First, Second in ``orders``, Elsewhere in ``support``) and a test of
    another org."""

    org_id: uuid.UUID
    orders: uuid.UUID
    support: uuid.UUID
    first: uuid.UUID
    second: uuid.UUID
    elsewhere: uuid.UUID
    foreign: uuid.UUID


async def seeded(database: Database) -> Seeded:
    org_id, other = await OrgFactory(database).create(), await OrgFactory(database).create()
    orders, support = await pathway(database, org_id), await pathway(database, org_id)
    first = await unit_test(database, org_id, orders, "First")
    second = await unit_test(database, org_id, orders, "Second")
    elsewhere = await unit_test(database, org_id, support, "Elsewhere")
    foreign = await unit_test(database, other, await pathway(database, other), "Foreign")
    return Seeded(org_id, orders, support, first, second, elsewhere, foreign)


async def test_tests_are_scoped_to_their_pathway_and_org(database: Database) -> None:
    seed = await seeded(database)
    org_id, orders, support = seed.org_id, seed.orders, seed.support
    first, second, foreign = seed.first, seed.second, seed.foreign
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


async def test_ordered_ids_and_find_stay_in_the_pathway(database: Database) -> None:
    seed = await seeded(database)
    org_id, orders, first, second = seed.org_id, seed.orders, seed.first, seed.second
    third = await unit_test(database, org_id, orders, "Third")
    elsewhere, foreign = seed.elsewhere, seed.foreign
    async with database.tenant_session(org_id) as session:
        repo = PathwayTestRepo(session, org_id)
        assert await repo.ordered_ids(orders, limit=10) == [first, second, third]
        assert await repo.ordered_ids(orders, limit=2) == [first, second]
        found = await repo.find(orders, [third, elsewhere, foreign, first])
        assert sorted(found) == sorted([first, third])
        assert found[first].name == "First"
        assert await repo.find(orders, []) == {}


NOW = datetime(2026, 11, 4, 9, 30, tzinfo=UTC)


def _item(test_id: str, status: str, **extra: object) -> dict[str, object]:
    return {"test_id": test_id, "status": status, **extra}


async def test_a_run_goes_queued_running_finished(database: Database) -> None:
    org_id = await OrgFactory(database).create()
    orders = await pathway(database, org_id)
    async with database.tenant_session(org_id) as session:
        added = await PathwayRunRepo(session, org_id).add(orders, version_number=3, kind="unit")
        assert (added.status, added.results, added.version_number, added.kind) == ("queued", [], 3, "unit")
    run_id = added.id
    async with database.tenant_session(org_id) as session:
        repo = PathwayRunRepo(session, org_id)
        assert await repo.begin(run_id, NOW)
        assert await repo.begin(run_id, NOW + timedelta(minutes=1))
        assert await repo.put_result(run_id, _item("b", "passed", attempt=1))
        assert await repo.put_result(run_id, _item("a", "failed"))
        assert await repo.put_result(run_id, _item("b", "passed", attempt=2))
        assert await repo.put_result(run_id, _item("b", "error"), replace=False)
    async with database.tenant_session(org_id) as session:
        repo = PathwayRunRepo(session, org_id)
        stored = await repo.get(orders, run_id)
        assert (stored.status, stored.started_at) == ("running", NOW)
        assert stored.results == [_item("a", "failed"), _item("b", "passed", attempt=2)]
        assert await repo.finish(run_id, ["b", "a"], NOW + timedelta(minutes=2)) == "failed"
    async with database.tenant_session(org_id) as session:
        repo = PathwayRunRepo(session, org_id)
        done = await repo.get(orders, run_id)
        assert (done.status, done.finished_at) == ("failed", NOW + timedelta(minutes=2))
        assert [item["test_id"] for item in done.results] == ["b", "a"]
        assert not await repo.begin(run_id, NOW)
        assert not await repo.put_result(run_id, _item("c", "passed"))
        assert await repo.finish(run_id, ["b", "a"], NOW) is None
        assert not await repo.fail(run_id, NOW)
        assert await repo.lock(uuid.uuid4()) is None
        assert not await repo.begin(uuid.uuid4(), NOW)


async def test_a_missing_result_makes_the_run_an_error_and_a_failed_start_marks_it(
    database: Database,
) -> None:
    org_id, other = await OrgFactory(database).create(), await OrgFactory(database).create()
    orders = await pathway(database, org_id)
    async with database.tenant_session(org_id) as session:
        repo = PathwayRunRepo(session, org_id)
        partial = await repo.add(orders, version_number=1, kind="unit")
        never = await repo.add(orders, version_number=1, kind="unit")
        assert await repo.put_result(partial.id, _item("a", "passed"))
        assert await repo.finish(partial.id, ["a", "b"], NOW) == "error"
        assert await repo.fail(never.id, NOW)
        assert (never.status, never.finished_at, never.started_at) == ("error", NOW, None)
    async with database.tenant_session(other) as session:
        assert await PathwayRunRepo(session, other).lock(never.id) is None
