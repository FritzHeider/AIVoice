"""``POST /v1/pathway/{id}/publish`` with the regression gate (specs/12 §4 step 2; Decision P07-T11e2): when
the suite runs, a blocking suite answers ``pending_regression`` and leaves the publish to the worker, a
reporting suite publishes at once next to its run, and start failures are handled per mode."""

from __future__ import annotations

import uuid
from typing import Any, Final

import httpx
import pytest
from identity_db import IdentityDb
from pathway_helpers import BASE, Api, add_unit_test, run_of, star_api
from session_helpers import SessionContainer, audit_rows, error_of
from sqlalchemy import select

from va_api.services import pathway_publish
from va_api.services.pathway_publish import SUITE_TOO_LARGE_MESSAGE
from va_api.services.pathway_test_runs import RunStartError
from va_db.models import PathwayTestRun
from va_db.pathway_cache import pathway_cache_key

pytestmark = pytest.mark.integration

TEST: Final[dict[str, Any]] = {"node_id": "start", "user_message": "Hello"}
SCENARIO: Final[dict[str, Any]] = {"name": "Caller", "persona": "In a hurry.", "goal": "Order placed"}


class Gate:
    def __init__(self, api: Api, container: SessionContainer, identity: IdentityDb, pathway_id: str) -> None:
        self.api = api
        self.container = container
        self.identity = identity
        self.pathway_id = pathway_id
        self.base = f"{BASE}/{pathway_id}"

    async def add_test(self, name: str) -> str:
        return await add_unit_test(self.api, self.pathway_id, {**TEST, "name": name})

    async def settings(self, **body: Any) -> None:
        response = await self.api.client.put(f"{self.base}/regression", json=body, headers=self.api.headers)
        assert response.status_code == 200, response.text

    async def publish(self, **body: Any) -> httpx.Response:
        return await self.api.publish(self.pathway_id, **body)

    async def run(self, run_id: str) -> dict[str, Any]:
        return run_of(await self.api.client.get(f"{self.base}/runs/{run_id}", headers=self.api.headers))

    async def production(self) -> int | None:
        detail = (await self.api.get(self.pathway_id)).json()
        versions = (await self.api.versions(self.pathway_id)).json()["versions"]
        assert detail["pathway_id"] == self.pathway_id
        pinned = [item["version_number"] for item in versions if item["is_production"]]
        return pinned[0] if pinned else None

    async def run_rows(self) -> list[PathwayTestRun]:
        async with self.identity.database.tenant_session(self.api.org_id) as session:
            return list(await session.scalars(select(PathwayTestRun)))

    async def publish_audits(self) -> int:
        return len(await audit_rows(self.identity, self.api.org_id, "pathway.publish"))

    @property
    def started(self) -> list[Any]:
        return self.container.pathway_test_starter.started


@pytest.fixture
async def gate(client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer) -> Gate:
    api = await star_api(client, identity, container)
    return Gate(api, container, identity, await api.created())


async def test_without_regression_the_publish_runs_nothing(gate: Gate) -> None:
    await gate.add_test("One")
    response = await gate.publish()
    assert response.json() == {
        "status": "success",
        "published_version": 1,
        "environment": "production",
        "version_number": 1,
        "draft_version": 2,
        "run_id": None,
    }
    assert (gate.started, await gate.run_rows()) == ([], [])


async def test_a_blocking_suite_answers_pending_and_publishes_nothing(gate: Gate) -> None:
    first, second = await gate.add_test("First"), await gate.add_test("Second")
    cache = pathway_cache_key(uuid.UUID(gate.pathway_id), "staging")
    await gate.container.redis.set(cache, "{}")
    response = await gate.publish(run_regression=True, environment="staging")
    assert response.status_code == 200, response.text
    body = response.json()
    run_id = body.pop("run_id")
    assert body == {
        "status": "pending_regression",
        "published_version": None,
        "environment": "staging",
        "version_number": 1,
        "draft_version": 1,
    }
    (started,) = gate.started
    assert (str(started.run_id), started.kind, started.version_number) == (run_id, "regression", 1)
    assert [str(test_id) for test_id in started.test_ids] == [first, second]
    assert started.simulations == ()
    intent = started.publish
    assert intent is not None
    detail = (await gate.api.get(gate.pathway_id)).json()
    assert (intent.environment, intent.version_number, intent.graph_hash) == (
        "staging",
        1,
        detail["graph_hash"],
    )
    assert (intent.actor.org_id, intent.actor.actor_type) == (gate.api.org_id, "api_key")
    run = await gate.run(run_id)
    assert (run["kind"], run["status"], run["version_number"]) == ("regression", "queued", 1)
    assert run["publish"] == {
        "block": True,
        "environment": "staging",
        "version_number": 1,
        "graph_hash": detail["graph_hash"],
        "outcome": "pending",
    }
    versions = (await gate.api.versions(gate.pathway_id)).json()["versions"]
    assert [(item["version_number"], item["status"]) for item in versions] == [(1, "draft")]
    assert await gate.publish_audits() == 0
    assert await gate.container.redis.exists(cache) == 1


async def test_the_suite_setting_alone_triggers_and_false_does_not_bypass_it(gate: Gate) -> None:
    await gate.add_test("One")
    await gate.settings(run_on_publish=True)
    response = await gate.publish(run_regression=False)
    assert response.json()["status"] == "pending_regression"
    assert len(gate.started) == 1


async def test_simulations_join_the_suite_once_per_scenario(gate: Gate) -> None:
    await gate.settings(include_simulations=True, scenarios=[SCENARIO, {**SCENARIO, "name": "Second"}])
    response = await gate.publish(run_regression=True)
    assert response.json()["status"] == "pending_regression"
    (started,) = gate.started
    assert started.test_ids == ()
    assert [(job.simulation_id, job.scenario.name) for job in started.simulations] == [
        ("1-1", "Caller"),
        ("2-1", "Second"),
    ]


async def test_a_reporting_suite_publishes_now_next_to_its_run(gate: Gate) -> None:
    await gate.add_test("One")
    await gate.settings(block_publish_on_failure=False)
    response = await gate.publish(run_regression=True)
    body = response.json()
    assert (body["status"], body["published_version"], body["version_number"], body["draft_version"]) == (
        "success",
        1,
        1,
        2,
    )
    assert await gate.production() == 1
    assert await gate.publish_audits() == 1
    (started,) = gate.started
    assert (str(started.run_id), started.kind, started.publish) == (body["run_id"], "regression", None)
    run = await gate.run(body["run_id"])
    assert run["publish"]["outcome"] == "published"
    assert (
        run["publish"]["block"],
        run["publish"]["published_version"],
        run["publish"]["draft_version"],
    ) == (
        False,
        1,
        2,
    )


async def test_an_empty_suite_publishes_at_once(gate: Gate) -> None:
    await gate.settings(include_simulations=True)
    response = await gate.publish(run_regression=True)
    assert (response.json()["status"], response.json()["run_id"]) == ("success", None)
    assert await gate.production() == 1
    assert (gate.started, await gate.run_rows()) == ([], [])


async def test_a_blocking_start_failure_publishes_nothing(gate: Gate) -> None:
    await gate.add_test("One")
    gate.container.pathway_test_starter.fail = RunStartError("temporal down")
    response = await gate.publish(run_regression=True)
    assert error_of(response)[:2] == (503, "SERVICE_UNAVAILABLE")
    assert [row.status for row in await gate.run_rows()] == ["error"]
    assert await gate.production() is None


async def test_a_reporting_start_failure_keeps_the_publish(gate: Gate) -> None:
    await gate.add_test("One")
    await gate.settings(block_publish_on_failure=False)
    gate.container.pathway_test_starter.fail = RunStartError("temporal down")
    response = await gate.publish(run_regression=True)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "success"
    assert [row.status for row in await gate.run_rows()] == ["error"]
    assert await gate.production() == 1


async def test_validation_comes_before_any_run(gate: Gate) -> None:
    await gate.add_test("One")
    broken = await gate.api.update(gate.pathway_id, nodes=[], edges=[])
    assert broken.status_code == 200, broken.text
    response = await gate.publish(run_regression=True)
    assert error_of(response)[:2] == (400, "INVALID_INPUT")
    assert (gate.started, await gate.run_rows()) == ([], [])


async def test_a_suite_above_the_test_cap_is_refused(gate: Gate, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pathway_publish, "MAX_RUN_TESTS", 1)
    await gate.add_test("One")
    await gate.add_test("Two")
    response = await gate.publish(run_regression=True)
    assert error_of(response) == (400, "INVALID_INPUT", "run_regression")
    assert response.json()["errors"][0]["message"] == SUITE_TOO_LARGE_MESSAGE
    assert (gate.started, await gate.run_rows()) == ([], [])
