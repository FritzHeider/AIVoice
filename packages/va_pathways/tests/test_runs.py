"""``run_stored_test``: a stored unit test against a stored pathway version, and the error items for data that
disappeared (specs/12 §8; Decision P07-T11c)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from pathways_helpers import TextRig, store_bella_pasta, store_unit_test, text_rig
from tenant_rows import OrgFactory, open_database

from va_common.settings import Environment
from va_db.org_settings import OrgSettingsReader
from va_db.repositories import PathwayTestRepo
from va_db.session import Database
from va_llm.tokens import default_counter
from va_pathways.runs import StoredTest, error_item, insecure_urls_allowed, run_stored_test

pytestmark = pytest.mark.integration

ROUTE_TO_BYE = (
    "rules:\n  - id: route\n    match: {tool_name: select_path}\n    respond:\n"
    "      tool_calls: [{name: select_path, arguments: {path_id: e_bye, confidence: 9,"
    " alternatives: [], reason: scripted}}]\n"
)


@pytest.fixture
async def rig() -> AsyncIterator[TextRig]:
    async with text_rig() as running:
        yield running


@pytest.fixture
async def database(pathways_db_url: str) -> AsyncIterator[Database]:
    async with open_database(pathways_db_url) as db:
        yield db


async def _run(
    rig: TextRig, database: Database, org_id: uuid.UUID, pathway_id: uuid.UUID, test_id: uuid.UUID
) -> Any:
    reader = OrgSettingsReader(database, FakeAsyncRedis())
    test = StoredTest(org_id=org_id, pathway_id=pathway_id, version_number=1, test_id=test_id)
    return await run_stored_test(rig.deps, database, reader, test, counter=default_counter())


async def test_a_stored_test_runs_against_the_stored_version(rig: TextRig, database: Database) -> None:
    org_id = await OrgFactory(database).create()
    pathway_id = await store_bella_pasta(database, org_id)
    passing = await store_unit_test(database, org_id, pathway_id)
    item = await _run(rig, database, org_id, pathway_id, passing)
    assert (item["test_id"], item["name"], item["status"], item["passed"]) == (
        str(passing),
        "Routes to order",
        "passed",
        True,
    )
    assert [case["route"] for case in item["cases"]] == ["order"]
    await rig.rules(ROUTE_TO_BYE)
    failing = await store_unit_test(database, org_id, pathway_id, name="Says bye")
    failed = await _run(rig, database, org_id, pathway_id, failing)
    assert (failed["status"], failed["cases"][0]["route"]) == ("failed", "bye")


async def test_missing_data_becomes_an_error_item(rig: TextRig, database: Database) -> None:
    org_id, other = await OrgFactory(database).create(), await OrgFactory(database).create()
    pathway_id = await store_bella_pasta(database, org_id)
    test_id = await store_unit_test(database, org_id, pathway_id)
    absent = await _run(rig, database, org_id, pathway_id, uuid.uuid4())
    assert absent["error"] == "test_not_found"
    assert (await _run(rig, database, other, pathway_id, test_id))["error"] == "test_not_found"
    reader = OrgSettingsReader(database, FakeAsyncRedis())
    wrong_version = StoredTest(org_id=org_id, pathway_id=pathway_id, version_number=9, test_id=test_id)
    item = await run_stored_test(rig.deps, database, reader, wrong_version, counter=default_counter())
    assert item == error_item(test_id, "Routes to order", "version_unavailable")
    async with database.tenant_session(org_id) as session:
        await PathwayTestRepo(session, org_id).change(
            await PathwayTestRepo(session, org_id).get(pathway_id, test_id), {"history": [{"role": "user"}]}
        )
    assert (await _run(rig, database, org_id, pathway_id, test_id))["error"] == "invalid_test"
    assert await rig.requests() == []


def test_error_items_and_insecure_environments() -> None:
    test_id = uuid.uuid4()
    assert error_item(test_id, None, "runner_error") == {
        "test_id": str(test_id),
        "name": None,
        "passed": False,
        "status": "error",
        "cases": [],
        "duration_ms": 0,
        "error": "runner_error",
    }
    assert [env for env in Environment if insecure_urls_allowed(env)] == [Environment.LOCAL, Environment.TEST]
