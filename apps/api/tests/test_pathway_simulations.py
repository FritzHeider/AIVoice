"""``POST /v1/pathway/{pathway_id}/simulations``: scenario validation, version resolution, the queued
simulation run, and the ``PathwayTestWorkflow`` start with one job per conversation (specs/12 §9; Decision
P07-T11d)."""

from __future__ import annotations

import uuid
from typing import Any, Final

import httpx
import pytest
from identity_db import IdentityDb
from pathway_helpers import BASE, Api, run_of, star_api
from session_helpers import SessionContainer, error_of
from sqlalchemy import select

from va_api.services.pathway_test_runs import RUNS_UNAVAILABLE_MESSAGE, RunStartError
from va_db.models import PathwayTestRun

pytestmark = pytest.mark.integration

SCENARIO: Final[dict[str, Any]] = {
    "name": "Impatient caller",
    "persona": "You are an impatient customer who wants a large pizza delivered fast.",
    "goal": "Order placed with address captured",
}


class Simulations:
    def __init__(self, api: Api, pathway_id: str) -> None:
        self.api = api
        self.pathway_id = pathway_id
        self.base = f"{BASE}/{pathway_id}"

    async def start(self, body: dict[str, Any], headers: dict[str, str] | None = None) -> httpx.Response:
        return await self.api.client.post(
            f"{self.base}/simulations", json=body, headers={**self.api.headers, **(headers or {})}
        )

    async def started(self, body: dict[str, Any]) -> dict[str, Any]:
        return run_of(await self.start(body))

    async def get(self, path: str) -> dict[str, Any]:
        response = await self.api.client.get(f"{self.base}{path}", headers=self.api.headers)
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body


@pytest.fixture
async def api(client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer) -> Api:
    return await star_api(client, identity, container)


@pytest.fixture
async def sims(api: Api) -> Simulations:
    return Simulations(api, await api.created())


async def test_scenarios_expand_into_one_job_per_conversation(
    sims: Simulations, container: SessionContainer
) -> None:
    second = {**SCENARIO, "name": "Calm caller", "max_turns": 5, "request_data": {"customer": "Ann"}}
    run = await sims.started({"scenarios": [SCENARIO, second], "runs_per_scenario": 3})
    assert (run["kind"], run["status"], run["version_number"], run["results"]) == (
        "simulation",
        "queued",
        1,
        [],
    )
    (started,) = container.pathway_test_starter.started
    assert (started.kind, started.test_ids, started.run_id) == ("simulation", (), uuid.UUID(run["run_id"]))
    assert (started.org_id, str(started.pathway_id), started.version_number) == (
        sims.api.org_id,
        sims.pathway_id,
        1,
    )
    assert [job.simulation_id for job in started.simulations] == ["1-1", "1-2", "1-3", "2-1", "2-2", "2-3"]
    first_job, last_job = started.simulations[0], started.simulations[-1]
    assert first_job.scenario.model_dump() == {**SCENARIO, "max_turns": 20, "request_data": {}}
    assert (last_job.scenario.name, last_job.scenario.max_turns, last_job.scenario.request_data) == (
        "Calm caller",
        5,
        {"customer": "Ann"},
    )
    assert (await sims.get(f"/runs/{run['run_id']}"))["run"]["kind"] == "simulation"
    listed = await sims.get("/runs?kind=simulation")
    assert [item["run_id"] for item in listed["runs"]] == [run["run_id"]]
    assert (await sims.get("/runs?kind=unit"))["runs"] == []


async def test_defaults_and_a_saved_version(sims: Simulations, container: SessionContainer) -> None:
    saved = await sims.api.save(sims.pathway_id)
    assert saved.status_code == 200, saved.text
    run = await sims.started({"version": 1, "scenarios": [SCENARIO]})
    assert run["version_number"] == 1
    (started,) = container.pathway_test_starter.started
    assert [job.simulation_id for job in started.simulations] == ["1-1"]


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"scenarios": []}, "scenarios"),
        ({"scenarios": [SCENARIO] * 21}, "scenarios"),
        ({"scenarios": [SCENARIO], "runs_per_scenario": 11}, "runs_per_scenario"),
        ({"scenarios": [SCENARIO], "runs_per_scenario": 0}, "runs_per_scenario"),
        ({"scenarios": [{**SCENARIO, "max_turns": 0}]}, "scenarios[0].max_turns"),
        ({"scenarios": [{**SCENARIO, "max_turns": 51}]}, "scenarios[0].max_turns"),
        ({"scenarios": [{**SCENARIO, "persona": "  "}]}, "scenarios[0].persona"),
        ({"scenarios": [{**SCENARIO, "mood": "angry"}]}, "scenarios[0].mood"),
        ({"scenarios": [SCENARIO], "tests": []}, "tests"),
        ({}, "scenarios"),
    ],
    ids=[
        "no_scenarios",
        "too_many_scenarios",
        "too_many_runs",
        "no_runs",
        "no_turns",
        "too_many_turns",
        "blank_persona",
        "unknown_scenario_key",
        "unknown_key",
        "missing_scenarios",
    ],
)
async def test_bodies_are_validated(
    sims: Simulations, container: SessionContainer, body: dict[str, Any], field: str
) -> None:
    assert error_of(await sims.start(body)) == (400, "INVALID_INPUT", field)
    assert container.pathway_test_starter.started == []


async def test_unknown_versions_and_pathways_are_refused(
    sims: Simulations, api: Api, container: SessionContainer
) -> None:
    body = {"scenarios": [SCENARIO]}
    assert error_of(await sims.start({**body, "version": "production"})) == (
        404,
        "PATHWAY_VERSION_NOT_FOUND",
        "version",
    )
    missing = await api.client.post(f"{BASE}/{uuid.uuid4()}/simulations", json=body, headers=api.headers)
    assert error_of(missing)[:2] == (404, "NOT_FOUND")
    assert container.pathway_test_starter.started == []


async def test_a_failed_start_marks_the_simulation_run_error(
    sims: Simulations, container: SessionContainer, identity: IdentityDb
) -> None:
    container.pathway_test_starter.fail = RunStartError("temporal down")
    response = await sims.start({"scenarios": [SCENARIO]})
    assert error_of(response)[:2] == (503, "SERVICE_UNAVAILABLE")
    assert response.json()["errors"][0]["message"] == RUNS_UNAVAILABLE_MESSAGE
    async with identity.database.tenant_session(sims.api.org_id) as session:
        rows = list(await session.scalars(select(PathwayTestRun)))
    assert [(row.kind, row.status, row.finished_at is not None) for row in rows] == [
        ("simulation", "error", True)
    ]


async def test_an_idempotent_replay_returns_the_same_simulation_run(
    sims: Simulations, container: SessionContainer
) -> None:
    key = {"Idempotency-Key": f"sim-{uuid.uuid4()}"}
    first = await sims.start({"scenarios": [SCENARIO]}, headers=key)
    replay = await sims.start({"scenarios": [SCENARIO]}, headers=key)
    assert first.status_code == replay.status_code == 200
    assert first.json()["run"]["run_id"] == replay.json()["run"]["run_id"]
    assert len(container.pathway_test_starter.started) == 1
