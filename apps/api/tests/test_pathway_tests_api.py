"""Pathway unit tests CRUD and the runs read API (specs/12 §8; Decision P07-T11b). ``from_call`` lives in
``test_pathway_tests_from_call.py``, roles in ``test_pathways_permissions.py``."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Final

import httpx
import pytest
from identity_db import IdentityDb
from pathway_helpers import BASE, Api, add, star_api
from session_helpers import SessionContainer, audit_rows, error_of

from va_api.routers.pathway_tests_schemas import run_summary
from va_api.services.pathway_tests import TEST_DELETED_MESSAGE
from va_db.models import PathwayTestRun, PathwayTestRunKind

pytestmark = pytest.mark.integration

BODY: Final[dict[str, Any]] = {
    "name": "Routes to order",
    "node_id": "start",
    "history": [{"role": "assistant", "content": "Hi, Bella Pasta."}],
    "user_message": "  Can I get a large pepperoni?  ",
    "expected": {"route_to_node_id": "node_order", "variables": {"pizza_size": "large"}},
    "simulated_variations": 3,
}


class Routes:
    """The unit test routes of one pathway."""

    def __init__(self, api: Api, pathway_id: str) -> None:
        self.api = api
        self.base = f"{BASE}/{pathway_id}"

    async def create(self, **override: Any) -> httpx.Response:
        return await self.api.client.post(
            f"{self.base}/tests", json={**BODY, **override}, headers=self.api.headers
        )

    async def created(self, **override: Any) -> dict[str, Any]:
        response = await self.create(**override)
        assert response.status_code == 200, response.text
        test: dict[str, Any] = response.json()["test"]
        return test

    async def get(self, test_id: str) -> httpx.Response:
        return await self.api.client.get(f"{self.base}/tests/{test_id}", headers=self.api.headers)

    async def list(self, **params: str) -> httpx.Response:
        return await self.api.client.get(f"{self.base}/tests", params=params, headers=self.api.headers)

    async def patch(self, test_id: str, **body: Any) -> httpx.Response:
        return await self.api.client.patch(
            f"{self.base}/tests/{test_id}", json=body, headers=self.api.headers
        )

    async def delete(self, test_id: str) -> httpx.Response:
        return await self.api.client.delete(f"{self.base}/tests/{test_id}", headers=self.api.headers)

    async def runs(self, **params: str) -> httpx.Response:
        return await self.api.client.get(f"{self.base}/runs", params=params, headers=self.api.headers)

    async def run(self, run_id: str) -> httpx.Response:
        return await self.api.client.get(f"{self.base}/runs/{run_id}", headers=self.api.headers)


def failure(response: httpx.Response) -> tuple[int, str, str]:
    """The status, error code, and message of the first error."""
    item = response.json()["errors"][0]
    return response.status_code, item["error"], item["message"]


@pytest.fixture
async def api(client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer) -> Api:
    return await star_api(client, identity, container)


@pytest.fixture
async def routes(api: Api) -> Routes:
    return Routes(api, await api.created())


async def test_create_get_list_patch_delete(routes: Routes, api: Api, identity: IdentityDb) -> None:
    test = await routes.created()
    assert set(test) == {
        "test_id",
        "pathway_id",
        "name",
        "node_id",
        "history",
        "user_message",
        "expected",
        "simulated_variations",
        "source_call_id",
        "created_at",
        "updated_at",
    }
    assert test["user_message"] == "Can I get a large pepperoni?"
    assert test["expected"] == BODY["expected"]
    assert (test["simulated_variations"], test["source_call_id"]) == (3, None)
    assert (await routes.get(test["test_id"])).json()["test"] == test

    patched = await routes.patch(
        test["test_id"], name="Orders", expected={"grading_goal": "Be polite."}, node_id=None
    )
    assert patched.status_code == 200, patched.text
    after = patched.json()["test"]
    assert (after["name"], after["node_id"], after["expected"]) == (
        "Orders",
        "start",
        {"grading_goal": "Be polite."},
    )
    assert (await routes.patch(test["test_id"], name="Orders")).status_code == 200
    updates = await audit_rows(identity, api.org_id, "pathway_test.update")
    assert [(row.changes["before"], row.changes["after"]) for row in updates] == [
        (
            {"name": "Routes to order", "expected_keys": ["route_to_node_id", "variables"]},
            {"name": "Orders", "expected_keys": ["grading_goal"], "fields": ["expected", "name"]},
        )
    ]
    (created,) = await audit_rows(identity, api.org_id, "pathway_test.create")
    assert created.resource_id == test["test_id"]
    assert created.changes["after"] == {
        "name": "Routes to order",
        "node_id": "start",
        "history_messages": 1,
        "expected_keys": ["route_to_node_id", "variables"],
        "simulated_variations": 3,
        "source_call_id": None,
    }

    deleted = await routes.delete(test["test_id"])
    assert deleted.json() == {"status": "success", "message": TEST_DELETED_MESSAGE}
    assert failure(await routes.get(test["test_id"])) == (404, "NOT_FOUND", "Pathway test not found.")
    assert error_of(await routes.delete(test["test_id"]))[:2] == (404, "NOT_FOUND")
    assert len(await audit_rows(identity, api.org_id, "pathway_test.delete")) == 1


async def test_defaults_and_pagination(routes: Routes) -> None:
    body = {"name": "Bare", "node_id": "start", "user_message": "Hello"}
    response = await routes.api.client.post(f"{routes.base}/tests", json=body, headers=routes.api.headers)
    assert response.status_code == 200, response.text
    bare = response.json()["test"]
    assert (bare["history"], bare["expected"], bare["simulated_variations"]) == ([], {}, 5)
    await routes.created()
    await routes.created()
    page = (await routes.list(limit="2")).json()
    assert len(page["tests"]) == 2
    rest = (await routes.list(limit="2", cursor=page["next_cursor"])).json()
    assert (len(rest["tests"]), rest["next_cursor"]) == (1, None)
    assert rest["tests"][0]["test_id"] == bare["test_id"]
    assert error_of(await routes.list(cursor="nope"))[:2] == (400, "INVALID_INPUT")


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"history": [{"role": "system", "content": "x"}]}, "history[0].role"),
        ({"user_message": "   "}, "user_message"),
        ({"simulated_variations": 21}, "simulated_variations"),
        ({"expected": {"route_to": "x"}}, "expected.route_to"),
        ({"expected": {"mock_tools": {"hook": {"status": 99}}}}, "expected.mock_tools.hook.status"),
        ({"node_id": ""}, "node_id"),
        ({"history": [{"role": "user", "content": "x"}] * 201}, "history"),
    ],
)
async def test_invalid_bodies(routes: Routes, override: dict[str, Any], field: str) -> None:
    response = await routes.create(**override)
    assert error_of(response) == (400, "INVALID_INPUT", field)


async def test_other_pathway_and_deleted_pathway(routes: Routes, api: Api) -> None:
    test = await routes.created()
    other = Routes(api, await api.created(name="Other"))
    assert failure(await other.get(test["test_id"])) == (404, "NOT_FOUND", "Pathway test not found.")
    assert error_of(await other.patch(test["test_id"], name="x"))[:2] == (404, "NOT_FOUND")
    pathway_id = routes.base.rsplit("/", 1)[1]
    assert (await api.delete(pathway_id)).status_code == 200
    assert failure(await routes.get(test["test_id"])) == (404, "NOT_FOUND", "Pathway not found.")
    assert failure(await routes.list()) == (404, "NOT_FOUND", "Pathway not found.")
    assert error_of(await routes.create())[:2] == (404, "NOT_FOUND")


async def _run(
    identity: IdentityDb, api: Api, pathway_id: str, kind: PathwayTestRunKind, results: list[Any]
) -> str:
    run = PathwayTestRun(
        org_id=api.org_id,
        pathway_id=uuid.UUID(pathway_id),
        version_number=2,
        kind=kind,
        status="failed",
        results=results,
        started_at=datetime(2026, 11, 4, 9, tzinfo=UTC),
    )
    await add(identity, api.org_id, run)
    return str(run.id)


async def test_runs_list_and_detail(routes: Routes, api: Api, identity: IdentityDb) -> None:
    pathway_id = routes.base.rsplit("/", 1)[1]
    results = [
        {"status": "passed"},
        {"status": "failed"},
        {"status": "error"},
        {"status": "passed"},
        {"x": 1},
    ]
    unit = await _run(identity, api, pathway_id, "unit", results)
    simulation = await _run(identity, api, pathway_id, "simulation", [])

    listed = (await routes.runs()).json()
    assert [item["run_id"] for item in listed["runs"]] == [simulation, unit]
    item = listed["runs"][1]
    assert "results" not in item
    assert item["summary"] == {"total": 5, "passed": 2, "failed": 1, "error": 1}
    assert (item["kind"], item["status"], item["version_number"]) == ("unit", "failed", 2)
    assert item["started_at"] == "2026-11-04T09:00:00Z"
    assert item["finished_at"] is None
    only_unit = (await routes.runs(kind="unit")).json()["runs"]
    assert [run["run_id"] for run in only_unit] == [unit]
    assert error_of(await routes.runs(kind="nightly"))[:2] == (400, "INVALID_INPUT")
    page = (await routes.runs(limit="1")).json()
    assert len(page["runs"]) == 1
    assert page["next_cursor"] is not None

    detail = (await routes.run(unit)).json()["run"]
    assert detail["results"] == results
    assert detail["summary"]["total"] == 5
    other = Routes(api, await api.created(name="Other"))
    assert failure(await other.run(unit)) == (404, "NOT_FOUND", "Pathway test run not found.")
    assert error_of(await routes.run(str(uuid.uuid4())))[:2] == (404, "NOT_FOUND")


def test_run_summary_counts_known_statuses() -> None:
    summary = run_summary([{"status": "passed"}, "junk", {"status": "queued"}, {"status": "error"}])
    assert summary.model_dump() == {"total": 4, "passed": 1, "failed": 0, "error": 1}
