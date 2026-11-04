"""The synchronous regression gate of ``POST /v1/pathway/{id}/publish`` (specs/12 §4 step 2; Decision
P07-T11e3): a blocking suite of at most 20 unit tests and no simulations runs in the request (8 at a time,
120 s cap) and answers 200 ``success`` or 409 ``CONFLICT`` with ``details.run_id``. Larger suites, suites with
simulations, and reporting suites keep the asynchronous path. The last test runs the real runner against
FakeLLM (it routes by edge label words)."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from typing import Any, Final

import httpx
import pytest
from call_fakes import FakePathwayTestRunner
from chat_helpers import ChatStack, WallClock, chat_stack
from graph_helpers import edge, node, start
from identity_db import IdentityDb
from publish_helpers import SCENARIO, TEST, Gate, gate_of
from session_helpers import SessionContainer, error_of
from sqlalchemy import update

from va_api.services import pathway_publish
from va_api.services.pathway_regression_sync import CONFLICT_MESSAGES, SYNC_CONCURRENCY, SYNC_MAX_TESTS
from va_db.models import PathwayUnitTest, PathwayVersion
from va_db.pathway_cache import pathway_cache_key

pytestmark = pytest.mark.integration

ORDER_TEST: Final[dict[str, Any]] = {
    "name": "Orders route",
    "node_id": "greet",
    "user_message": "I want to order food please",
    "expected": {"route_to_node_id": "order"},
    "simulated_variations": 0,
}


@pytest.fixture
def container(identity: IdentityDb) -> SessionContainer:
    """The suite timeout runs on the container clock: wall clock here."""
    return SessionContainer(identity=identity, clock=WallClock())


@pytest.fixture
def runner(container: SessionContainer, client: httpx.AsyncClient) -> FakePathwayTestRunner:
    # ``client`` opened the app, which built the real runner: replace it afterwards.
    assert client is not None
    fake = FakePathwayTestRunner()
    container.pathway_test_runner = fake
    return fake


@pytest.fixture
async def gate(
    client: httpx.AsyncClient,
    identity: IdentityDb,
    container: SessionContainer,
    runner: FakePathwayTestRunner,
) -> Gate:
    assert runner is container.pathway_test_runner
    return await gate_of(client, identity, container)


async def conflict(response: httpx.Response) -> tuple[str, str, str]:
    """``(run_id, outcome, message)`` of a 409 publish."""
    assert error_of(response)[:2] == (409, "CONFLICT"), response.text
    (item,) = response.json()["errors"]
    return item["details"]["run_id"], item["details"]["outcome"], item["message"]


async def test_a_passing_suite_publishes_in_the_request(gate: Gate, runner: FakePathwayTestRunner) -> None:
    first, second = await gate.add_test("First"), await gate.add_test("Second")
    cache = pathway_cache_key(uuid.UUID(gate.pathway_id), "production")
    await gate.container.redis.set(cache, "{}")
    response = await gate.publish(run_regression=True)
    assert response.status_code == 200, response.text
    body = response.json()
    run_id = body.pop("run_id")
    assert body == {
        "status": "success",
        "published_version": 1,
        "environment": "production",
        "version_number": 1,
        "draft_version": 2,
    }
    assert sorted(runner.ran) == sorted([first, second])
    run = await gate.run(run_id)
    assert (run["kind"], run["status"], run["version_number"]) == ("regression", "passed", 1)
    assert [item["test_id"] for item in run["results"]] == [first, second]
    assert (run["publish"]["outcome"], run["publish"]["published_version"]) == ("published", 1)
    assert run["started_at"] is not None
    assert run["finished_at"] is not None
    assert await gate.production() == 1
    assert await gate.publish_audits() == 1
    assert gate.started == []
    assert await gate.container.redis.exists(cache) == 0


async def test_a_failing_test_answers_409_and_publishes_nothing(
    gate: Gate, runner: FakePathwayTestRunner
) -> None:
    await gate.add_test("Passes")
    failing = await gate.add_test("Fails")
    runner.script[failing] = "failed"
    cache = pathway_cache_key(uuid.UUID(gate.pathway_id), "production")
    await gate.container.redis.set(cache, "{}")
    response = await gate.publish(run_regression=True)
    run_id, outcome, message = await conflict(response)
    assert (outcome, message) == ("blocked", CONFLICT_MESSAGES["blocked"])
    run = await gate.run(run_id)
    assert (run["status"], run["publish"]["outcome"]) == ("failed", "blocked")
    assert "published_version" not in run["publish"]
    assert await gate.production() is None
    assert await gate.publish_audits() == 0
    assert await gate.container.redis.exists(cache) == 1
    versions = (await gate.api.versions(gate.pathway_id)).json()["versions"]
    assert [(item["version_number"], item["status"]) for item in versions] == [(1, "draft")]
    assert gate.started == []


async def test_the_suite_setting_alone_triggers_the_synchronous_gate(
    gate: Gate, runner: FakePathwayTestRunner
) -> None:
    failing = await gate.add_test("Fails")
    runner.script[failing] = "error"
    await gate.settings(run_on_publish=True)
    run_id, outcome, _ = await conflict(await gate.publish())
    assert outcome == "blocked"
    assert (await gate.run(run_id))["status"] == "error"


async def test_a_runner_exception_is_an_error_item(gate: Gate, runner: FakePathwayTestRunner) -> None:
    broken = await gate.add_test("Broken")
    runner.script[broken] = "raise"
    run_id, outcome, _ = await conflict(await gate.publish(run_regression=True))
    run = await gate.run(run_id)
    assert outcome == "blocked"
    assert run["status"] == "error"
    assert [(item["test_id"], item["status"], item["error"]) for item in run["results"]] == [
        (broken, "error", "runner_error")
    ]


async def test_the_suite_timeout_records_timeout_items(
    gate: Gate, runner: FakePathwayTestRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pathway_publish, "SYNC_TIMEOUT_S", 0.2)
    done = await gate.add_test("Done")
    stuck = await gate.add_test("Stuck")
    runner.script[stuck] = "hang"
    run_id, outcome, _ = await conflict(await gate.publish(run_regression=True))
    run = await gate.run(run_id)
    assert (outcome, run["status"], run["publish"]["outcome"]) == ("blocked", "error", "blocked")
    assert [(item["test_id"], item["status"], item.get("error")) for item in run["results"]] == [
        (done, "passed", None),
        (stuck, "error", "regression_timeout"),
    ]
    assert await gate.production() is None


async def test_a_draft_edited_during_the_run_is_stale(gate: Gate, runner: FakePathwayTestRunner) -> None:
    await gate.add_test("One")

    async def edit_draft() -> None:
        async with gate.identity.database.tenant_session(gate.api.org_id) as session:
            await session.execute(
                update(PathwayVersion)
                .where(PathwayVersion.pathway_id == uuid.UUID(gate.pathway_id))
                .values(graph_hash="f" * 64)
            )

    runner.before = edit_draft
    run_id, outcome, message = await conflict(await gate.publish(run_regression=True))
    assert (outcome, message) == ("stale", CONFLICT_MESSAGES["stale"])
    run = await gate.run(run_id)
    assert (run["status"], run["publish"]["outcome"]) == ("passed", "stale")
    assert await gate.production() is None


async def test_concurrency_is_capped_and_results_keep_test_order(
    gate: Gate, runner: FakePathwayTestRunner
) -> None:
    tests = [await gate.add_test(f"Test {index}") for index in range(SYNC_CONCURRENCY + 4)]
    release = asyncio.Event()
    runner.gate = release
    runner.saturate_at = SYNC_CONCURRENCY
    publish = asyncio.create_task(gate.publish(run_regression=True))
    await runner.saturated.wait()
    release.set()
    response = await publish
    assert response.status_code == 200, response.text
    assert runner.max_in_flight == SYNC_CONCURRENCY
    run = await gate.run(response.json()["run_id"])
    assert [item["test_id"] for item in run["results"]] == tests


async def test_a_cancelled_request_still_closes_the_run(gate: Gate, runner: FakePathwayTestRunner) -> None:
    stuck = await gate.add_test("Stuck")
    runner.script[stuck] = "hang"
    publish = asyncio.create_task(gate.publish(run_regression=True))
    await runner.started.wait()
    publish.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await publish
    (row,) = await gate.run_rows()
    assert row.status == "error"
    assert row.finished_at is not None
    assert isinstance(row.publish, dict)
    assert row.publish["outcome"] == "blocked"
    assert await gate.production() is None


async def seed_tests(gate: Gate, count: int) -> None:
    async with gate.identity.database.tenant_session(gate.api.org_id) as session:
        for index in range(count):
            session.add(
                PathwayUnitTest(
                    org_id=gate.api.org_id,
                    pathway_id=uuid.UUID(gate.pathway_id),
                    name=f"Seeded {index}",
                    node_id=TEST["node_id"],
                    history=[],
                    user_message=TEST["user_message"],
                    expected={},
                    simulated_variations=0,
                )
            )


async def test_more_than_twenty_tests_keep_the_asynchronous_path(
    gate: Gate, runner: FakePathwayTestRunner
) -> None:
    await seed_tests(gate, SYNC_MAX_TESTS + 1)
    response = await gate.publish(run_regression=True)
    assert response.json()["status"] == "pending_regression"
    (started,) = gate.started
    assert len(started.test_ids) == SYNC_MAX_TESTS + 1
    assert runner.ran == []


async def test_simulations_keep_the_asynchronous_path(gate: Gate, runner: FakePathwayTestRunner) -> None:
    await gate.add_test("One")
    await gate.settings(include_simulations=True, scenarios=[SCENARIO])
    response = await gate.publish(run_regression=True)
    assert response.json()["status"] == "pending_regression"
    assert len(gate.started) == 1
    assert runner.ran == []


async def test_a_reporting_suite_stays_asynchronous(gate: Gate, runner: FakePathwayTestRunner) -> None:
    await gate.add_test("One")
    await gate.settings(block_publish_on_failure=False)
    response = await gate.publish(run_regression=True)
    assert (response.json()["status"], response.json()["published_version"]) == ("success", 1)
    assert len(gate.started) == 1
    assert runner.ran == []


def ordering_graph(order_target: str) -> dict[str, Any]:
    """Greeting → ordering or goodbye; ``order_target`` is where the "order food" edge leads."""
    nodes = [
        start("greet", name="Greeting", text="Hi, thanks for calling Bella Pasta."),
        node("order", name="Take Order", text="What would you like to order?"),
        node("bye", name="Goodbye", node_type="End Call", endCall={"speech": "Goodbye!"}),
    ]
    edges = [
        edge("e_order", "greet", order_target, "Caller wants to order food"),
        edge("e_bye", "greet", "bye", "Caller says goodbye"),
    ]
    return {"nodes": nodes, "edges": edges, "global_config": {}}


@pytest.fixture
async def stack(container: SessionContainer) -> AsyncIterator[ChatStack]:
    async with chat_stack(container) as running:
        yield running


async def test_the_real_runner_blocks_a_broken_draft_and_publishes_the_fix(
    stack: ChatStack, identity: IdentityDb, container: SessionContainer
) -> None:
    gate = await gate_of(stack.client, identity, container, name="Bella Pasta", **ordering_graph("bye"))
    response = await gate.api.client.post(f"{gate.base}/tests", json=ORDER_TEST, headers=gate.api.headers)
    assert response.status_code == 200, response.text
    await gate.settings(run_on_publish=True, block_publish_on_failure=True)
    run_id, outcome, _ = await conflict(await gate.publish())
    run = await gate.run(run_id)
    assert (outcome, run["status"]) == ("blocked", "failed")
    assert await gate.production() is None
    graph = ordering_graph("order")
    # Nodes and edges only: the draft's ``global_config`` holds the suite settings.
    fixed = await gate.api.update(gate.pathway_id, nodes=graph["nodes"], edges=graph["edges"])
    assert fixed.status_code == 200, fixed.text
    published = await gate.publish()
    assert published.status_code == 200, published.text
    body = published.json()
    assert (body["status"], body["published_version"]) == ("success", 1)
    assert (await gate.run(body["run_id"]))["status"] == "passed"
    assert await gate.production() == 1
