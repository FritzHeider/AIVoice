"""``PathwayTestActivities`` on Postgres with FakeLLM: begin, one stored result per test (a retry replaces
it), runner errors, and the finish (specs/12 §8; Decision P07-T11c)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from fakeredis import FakeAsyncRedis
from pathways_helpers import TextRig, store_bella_pasta, store_unit_test, text_rig
from temporalio.testing import ActivityEnvironment
from tenant_rows import OrgFactory

from va_common.time import FrozenClock
from va_db.models import PathwayTestRun
from va_db.org_settings import OrgSettingsReader
from va_db.repositories import PathwayRunRepo
from va_db.session import Database
from va_llm.tokens import TokenCounter, default_counter
from va_workflows.activities.pathway_tests import PathwayTestActivities
from va_workflows.activities.pathway_tests_inputs import (
    FinishInput,
    RunRef,
    UnitTestErrorInput,
    UnitTestInput,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def rig() -> AsyncIterator[TextRig]:
    async with text_rig() as running:
        yield running


def activities(database: Database, rig: TextRig, counted: list[int]) -> PathwayTestActivities:
    def counter_factory() -> TokenCounter:
        counted.append(1)
        return default_counter()

    return PathwayTestActivities(
        database=database,
        org_settings=OrgSettingsReader(database, FakeAsyncRedis()),
        text=rig.deps,
        clock=FrozenClock(),
        counter_factory=counter_factory,
    )


async def stored_run(database: Database, org_id: uuid.UUID, run_id: uuid.UUID) -> PathwayTestRun:
    async with database.tenant_session(org_id) as session:
        run = await PathwayRunRepo(session, org_id).lock(run_id)
        assert run is not None
        return run


async def test_a_run_begins_stores_each_result_once_and_finishes(database: Database, rig: TextRig) -> None:
    org_id = await OrgFactory(database).create()
    pathway_id = await store_bella_pasta(database, org_id)
    passing = await store_unit_test(database, org_id, pathway_id)
    gone = uuid.uuid4()
    async with database.tenant_session(org_id) as session:
        run = await PathwayRunRepo(session, org_id).add(pathway_id, version_number=1, kind="unit")
    counted: list[int] = []
    acts = activities(database, rig, counted)
    env = ActivityEnvironment()
    ref = RunRef(run_id=run.id, org_id=org_id)
    assert await env.run(acts.begin_pathway_test_run, ref)
    assert (await stored_run(database, org_id, run.id)).status == "running"

    def test_input(test_id: uuid.UUID) -> UnitTestInput:
        return UnitTestInput(
            run_id=run.id, org_id=org_id, pathway_id=pathway_id, version_number=1, test_id=test_id
        )

    assert await env.run(acts.run_pathway_unit_test, test_input(passing)) == "passed"
    assert await env.run(acts.run_pathway_unit_test, test_input(passing)) == "passed"
    assert await env.run(acts.run_pathway_unit_test, test_input(gone)) == "error"
    errored = UnitTestErrorInput(run_id=run.id, org_id=org_id, test_id=passing)
    assert await env.run(acts.record_pathway_test_error, errored) == "error"
    assert counted == [1]
    mid = await stored_run(database, org_id, run.id)
    assert [(item["test_id"], item["status"]) for item in mid.results] == [
        (str(passing), "passed"),
        (str(gone), "error"),
    ]
    assert mid.results[1]["error"] == "test_not_found"

    finished = await env.run(
        acts.finish_pathway_test_run, FinishInput(**ref.model_dump(), test_ids=(gone, passing))
    )
    assert finished.status == "error"
    done = await stored_run(database, org_id, run.id)
    assert done.status == "error"
    assert [item["test_id"] for item in done.results] == [str(gone), str(passing)]
    assert done.finished_at is not None
    assert not await env.run(acts.begin_pathway_test_run, ref)
    assert await env.run(acts.run_pathway_unit_test, test_input(passing)) == "skipped"
    assert (
        await env.run(acts.finish_pathway_test_run, FinishInput(**ref.model_dump(), test_ids=()))
    ).status is None
