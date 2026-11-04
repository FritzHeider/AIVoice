"""``PathwayTestActivities`` on Postgres with FakeLLM: begin, one stored result per test or conversation (a
retry replaces it), runner errors, the finish, and the regression publish settle (specs/12 §4, §8, §9;
Decisions P07-T11c, P07-T11d, and P07-T11e2)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from fakeredis import FakeAsyncRedis
from pathway_rows import locked_run, publish_of, seed_regression_run
from pathways_helpers import TextRig, job, store_bella_pasta, store_unit_test, text_rig
from temporalio.testing import ActivityEnvironment
from tenant_rows import OrgFactory

from va_common.time import FrozenClock
from va_db.org_settings import OrgSettingsReader
from va_db.pathway_cache import pathway_cache_key
from va_db.repositories import PathwayRepo, PathwayRunRepo
from va_db.session import Database
from va_domain.pathway_tests import PublishActor, RegressionPublish
from va_llm.tokens import TokenCounter, default_counter
from va_workflows.activities.pathway_tests import PathwayTestActivities
from va_workflows.activities.pathway_tests_inputs import (
    FinishInput,
    RunRef,
    SettleInput,
    SettleResult,
    SimulationErrorInput,
    SimulationInput,
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
    assert (await locked_run(database, org_id, run.id)).status == "running"

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
    mid = await locked_run(database, org_id, run.id)
    assert [(item["test_id"], item["status"]) for item in mid.results] == [
        (str(passing), "passed"),
        (str(gone), "error"),
    ]
    assert mid.results[1]["error"] == "test_not_found"

    finished = await env.run(
        acts.finish_pathway_test_run, FinishInput(**ref.model_dump(), test_ids=(gone, passing))
    )
    assert finished.status == "error"
    done = await locked_run(database, org_id, run.id)
    assert done.status == "error"
    assert [item["test_id"] for item in done.results] == [str(gone), str(passing)]
    assert done.finished_at is not None
    assert not await env.run(acts.begin_pathway_test_run, ref)
    assert await env.run(acts.run_pathway_unit_test, test_input(passing)) == "skipped"
    assert (
        await env.run(acts.finish_pathway_test_run, FinishInput(**ref.model_dump(), test_ids=()))
    ).status is None


async def test_simulations_store_one_result_each_and_finish_in_job_order(
    database: Database, rig: TextRig
) -> None:
    org_id = await OrgFactory(database).create()
    pathway_id = await store_bella_pasta(database, org_id)
    async with database.tenant_session(org_id) as session:
        run = await PathwayRunRepo(session, org_id).add(pathway_id, version_number=1, kind="simulation")
    await rig.rules(
        "rules:\n  - id: caller\n    priority: 10\n    match: {system_contains: ['[HANGUP]']}\n"
        "    respond:\n      content: 'Bye [HANGUP]'\n"
        "  - id: grade\n    match: {tool_name: grade}\n    respond:\n"
        "      tool_calls: [{name: grade, arguments: {pass: true, rationale: ok}}]\n"
    )
    acts = activities(database, rig, [])
    env = ActivityEnvironment()
    ref = RunRef(run_id=run.id, org_id=org_id)
    assert await env.run(acts.begin_pathway_test_run, ref)
    first, second = job("1-1"), job("1-2")

    def sim_input(simulation: object) -> SimulationInput:
        return SimulationInput.model_validate(
            {**ref.model_dump(), "pathway_id": pathway_id, "version_number": 1, "job": simulation}
        )

    assert await env.run(acts.run_pathway_simulation, sim_input(second)) == "passed"
    assert await env.run(acts.run_pathway_simulation, sim_input(second)) == "passed"
    errored = SimulationErrorInput(**ref.model_dump(), job=second)
    assert await env.run(acts.record_pathway_simulation_error, errored) == "error"
    assert (
        await env.run(
            acts.record_pathway_simulation_error, SimulationErrorInput(**ref.model_dump(), job=first)
        )
        == "error"
    )
    finished = await env.run(
        acts.finish_pathway_test_run, FinishInput(**ref.model_dump(), simulation_ids=("1-1", "1-2"))
    )
    assert finished.status == "error"
    done = await locked_run(database, org_id, run.id)
    assert [(item["simulation_id"], item["status"]) for item in done.results] == [
        ("1-1", "error"),
        ("1-2", "passed"),
    ]
    assert done.results[0]["error"] == "runner_error"
    assert done.results[1]["ended_by"] == "hangup"


@pytest.mark.parametrize(("hash_matches", "outcome"), [(True, "published"), (False, "stale")])
async def test_settling_publishes_and_invalidates_the_cache_only_when_published(
    database: Database, rig: TextRig, hash_matches: bool, outcome: str
) -> None:
    org_id = await OrgFactory(database).create()
    pathway_id = await store_bella_pasta(database, org_id)
    async with database.tenant_session(org_id) as session:
        repo = PathwayRepo(session, org_id)
        graph_hash = (await repo.draft(await repo.get(pathway_id))).graph_hash
    wanted = RegressionPublish(
        environment="production",
        version_number=1,
        graph_hash=graph_hash if hash_matches else "f" * 64,
        actor=PublishActor(org_id=org_id, actor_type="api_key", actor_id=uuid.uuid4()),
    )
    run_id = await seed_regression_run(database, org_id, pathway_id, wanted, "passed")
    redis = FakeAsyncRedis()
    key = pathway_cache_key(pathway_id, "production")
    await redis.set(key, b"cached")
    acts = PathwayTestActivities(
        database=database,
        org_settings=OrgSettingsReader(database, redis),
        text=rig.deps,
        clock=FrozenClock(),
        redis=redis,
    )
    request = SettleInput(run_id=run_id, org_id=org_id, pathway_id=pathway_id, publish=wanted)
    settled = await ActivityEnvironment().run(acts.settle_regression_publish, request)
    assert settled == SettleResult(outcome=outcome)  # type: ignore[arg-type]
    assert (await publish_of(database, org_id, run_id))["outcome"] == outcome
    assert (await redis.exists(key)) == (0 if hash_matches else 1)
    gone = SettleInput(run_id=uuid.uuid4(), org_id=org_id, pathway_id=pathway_id, publish=wanted)
    assert await ActivityEnvironment().run(acts.settle_regression_publish, gone) == SettleResult(outcome=None)
