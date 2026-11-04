"""``POST /v1/pathway/{pathway_id}/tests/run``: version resolution, test selection, the queued run row, and
the ``PathwayTestWorkflow`` start (specs/12 §8; Decision P07-T11c)."""

from __future__ import annotations

import uuid
from typing import Any, Final

import httpx
import pytest
from identity_db import IdentityDb
from pathway_helpers import BASE, Api, star_api
from session_helpers import SessionContainer, error_of
from sqlalchemy import select

from va_api.services import pathway_test_runs
from va_api.services.pathway_test_runs import NO_TESTS_MESSAGE, RUNS_UNAVAILABLE_MESSAGE, RunStartError
from va_db.models import PathwayTestRun

pytestmark = pytest.mark.integration

TEST: Final[dict[str, Any]] = {"name": "Routes", "node_id": "start", "user_message": "Hello"}


class Runs:
    def __init__(self, api: Api, pathway_id: str) -> None:
        self.api = api
        self.pathway_id = pathway_id
        self.base = f"{BASE}/{pathway_id}"

    async def add_test(self, name: str = "Routes") -> str:
        response = await self.api.client.post(
            f"{self.base}/tests", json={**TEST, "name": name}, headers=self.api.headers
        )
        assert response.status_code == 200, response.text
        test_id: str = response.json()["test"]["test_id"]
        return test_id

    async def start(
        self, body: dict[str, Any] | None = None, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        return await self.api.client.post(
            f"{self.base}/tests/run", json=body or {}, headers={**self.api.headers, **(headers or {})}
        )

    async def started(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self.start(body)
        assert response.status_code == 200, response.text
        run: dict[str, Any] = response.json()["run"]
        return run

    async def read(self, run_id: str) -> dict[str, Any]:
        response = await self.api.client.get(f"{self.base}/runs/{run_id}", headers=self.api.headers)
        assert response.status_code == 200, response.text
        run: dict[str, Any] = response.json()["run"]
        return run


@pytest.fixture
async def api(client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer) -> Api:
    return await star_api(client, identity, container)


@pytest.fixture
async def runs(api: Api) -> Runs:
    return Runs(api, await api.created())


async def test_a_run_of_every_test_starts_queued_on_the_draft(
    runs: Runs, container: SessionContainer
) -> None:
    first, second = await runs.add_test("First"), await runs.add_test("Second")
    run = await runs.started()
    assert (run["kind"], run["status"], run["version_number"], run["results"]) == ("unit", "queued", 1, [])
    assert run["summary"] == {"total": 0, "passed": 0, "failed": 0, "error": 0}
    assert (run["started_at"], run["finished_at"]) == (None, None)
    (started,) = container.pathway_test_starter.started
    assert started.run_id == uuid.UUID(run["run_id"])
    assert (started.org_id, str(started.pathway_id), started.version_number) == (
        runs.api.org_id,
        runs.pathway_id,
        1,
    )
    assert [str(test_id) for test_id in started.test_ids] == [first, second]
    assert started.workflow_id() == f"pathway-test-run-{run['run_id']}"
    assert (await runs.read(run["run_id"]))["status"] == "queued"


async def test_given_ids_run_in_order_without_duplicates_on_a_version_number(
    runs: Runs, container: SessionContainer
) -> None:
    first, second = await runs.add_test("First"), await runs.add_test("Second")
    saved = await runs.api.save(runs.pathway_id)
    assert saved.status_code == 200, saved.text
    run = await runs.started({"version": 1, "test_ids": [second, first, second]})
    assert run["version_number"] == 1
    (started,) = container.pathway_test_starter.started
    assert [str(test_id) for test_id in started.test_ids] == [second, first]


async def test_unknown_tests_and_versions_are_refused(
    runs: Runs, api: Api, container: SessionContainer
) -> None:
    other = Runs(api, await api.created(name="Other"))
    foreign = await other.add_test()
    await runs.add_test()
    response = await runs.start({"test_ids": [foreign]})
    assert error_of(response)[:2] == (404, "NOT_FOUND")
    assert response.json()["errors"][0]["message"] == "Pathway test not found."
    assert error_of(await runs.start({"version": "production"})) == (
        404,
        "PATHWAY_VERSION_NOT_FOUND",
        "version",
    )
    assert error_of(await runs.start({"version": 9})) == (404, "PATHWAY_VERSION_NOT_FOUND", "version")
    assert error_of(await runs.start({"version": 0}))[:2] == (400, "INVALID_INPUT")
    missing = await api.client.post(f"{BASE}/{uuid.uuid4()}/tests/run", json={}, headers=api.headers)
    assert error_of(missing)[:2] == (404, "NOT_FOUND")
    assert container.pathway_test_starter.started == []


async def test_a_run_needs_tests(runs: Runs, monkeypatch: pytest.MonkeyPatch) -> None:
    response = await runs.start()
    assert error_of(response) == (400, "INVALID_INPUT", "test_ids")
    assert response.json()["errors"][0]["message"] == NO_TESTS_MESSAGE
    assert error_of(await runs.start({"test_ids": []})) == (400, "INVALID_INPUT", "test_ids")
    too_many = [str(uuid.uuid4()) for _ in range(501)]
    assert error_of(await runs.start({"test_ids": too_many})) == (400, "INVALID_INPUT", "test_ids")
    assert error_of(await runs.start({"tests": None}))[:2] == (400, "INVALID_INPUT")
    monkeypatch.setattr(pathway_test_runs, "MAX_RUN_TESTS", 2)
    for name in ("One", "Two", "Three"):
        await runs.add_test(name)
    capped = await runs.start()
    assert error_of(capped) == (400, "INVALID_INPUT", "test_ids")
    assert capped.json()["errors"][0]["message"] == "Select at most 500 tests."


async def test_a_failed_start_marks_the_run_error(
    runs: Runs, container: SessionContainer, identity: IdentityDb
) -> None:
    await runs.add_test()
    container.pathway_test_starter.fail = RunStartError("temporal down")
    response = await runs.start()
    assert error_of(response)[:2] == (503, "SERVICE_UNAVAILABLE")
    assert response.json()["errors"][0]["message"] == RUNS_UNAVAILABLE_MESSAGE
    async with identity.database.tenant_session(runs.api.org_id) as session:
        rows = list(await session.scalars(select(PathwayTestRun)))
    assert [(row.status, row.finished_at is not None) for row in rows] == [("error", True)]


async def test_an_idempotent_replay_returns_the_same_run(runs: Runs, container: SessionContainer) -> None:
    await runs.add_test()
    key = {"Idempotency-Key": f"run-{uuid.uuid4()}"}
    first = await runs.start(headers=key)
    replay = await runs.start(headers=key)
    assert first.status_code == replay.status_code == 200
    assert first.json()["run"]["run_id"] == replay.json()["run"]["run_id"]
    assert len(container.pathway_test_starter.started) == 1
