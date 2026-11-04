"""Pathway versions: list/get, save, publish, promote, revert, cache invalidation, audit (specs/12 §4;
Decision P07-T09b). Roles live in ``test_pathways_permissions.py``."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from graph_helpers import node, start
from identity_db import IdentityDb
from pathway_helpers import Api, star_api
from redis.exceptions import ConnectionError as RedisConnectionError
from service_helpers import json_log_stream, log_events
from session_helpers import SessionContainer, audit_rows, error_of
from sqlalchemy import select

from va_api.services.pathway_versions import NO_STAGING_MESSAGE
from va_db.models import PathwayVersion
from va_db.pathway_cache import pathway_cache_key

pytestmark = pytest.mark.integration


@pytest.fixture
async def api(client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer) -> Api:
    return await star_api(client, identity, container)


async def version_rows(identity: IdentityDb, pathway_id: str) -> list[PathwayVersion]:
    statement = (
        select(PathwayVersion)
        .where(PathwayVersion.pathway_id == uuid.UUID(pathway_id))
        .order_by(PathwayVersion.version_number)
    )
    async with identity.database.system_session() as session:
        return list(await session.scalars(statement))


async def assert_one_draft(api: Api, identity: IdentityDb, pathway_id: str) -> dict[str, Any]:
    """Exactly one draft row whose number is both draft_version and latest_version; returns the detail."""
    rows = await version_rows(identity, pathway_id)
    drafts = [row.version_number for row in rows if row.status == "draft"]
    detail: dict[str, Any] = (await api.get(pathway_id)).json()
    listed = next(item for item in (await api.list()).json()["pathways"] if item["pathway_id"] == pathway_id)
    assert drafts == [detail["version_number"]] == [listed["draft_version"]] == [listed["latest_version"]]
    assert drafts == [rows[-1].version_number]
    return detail


def summary(response: httpx.Response) -> list[tuple[int, str, bool, bool]]:
    assert response.status_code == 200, response.text
    return [
        (item["version_number"], item["status"], item["is_production"], item["is_staging"])
        for item in response.json()["versions"]
    ]


async def test_version_lifecycle_keeps_one_draft(api: Api, identity: IdentityDb) -> None:
    pathway_id = await api.created(nodes=[start(text="Hello")], edges=[])
    await api.update(pathway_id, nodes=[start(text="Hello v1")])

    saved = await api.save(pathway_id, {"name": "  v1 greeting  "})
    assert saved.json() == {"status": "success", "saved_version": 1, "draft_version": 2}
    await assert_one_draft(api, identity, pathway_id)
    assert summary(await api.versions(pathway_id)) == [
        (2, "draft", False, False),
        (1, "archived", False, False),
    ]
    first = (await api.versions(pathway_id)).json()["versions"][1]
    assert (first["name"], first["published_at"], first["published_by"]) == ("v1 greeting", None, None)

    published = await api.publish(pathway_id)
    assert published.json() == {
        "status": "success",
        "published_version": 2,
        "environment": "production",
        "version_number": 2,
        "draft_version": 3,
        "run_id": None,
    }
    await assert_one_draft(api, identity, pathway_id)
    await api.update(pathway_id, nodes=[start(text="Hello v3")])

    staged = await api.publish(pathway_id, environment="staging")
    assert (staged.json()["published_version"], staged.json()["draft_version"]) == (3, 4)
    assert summary(await api.versions(pathway_id)) == [
        (4, "draft", False, False),
        (3, "published", False, True),
        (2, "published", True, False),
        (1, "archived", False, False),
    ]

    promoted = await api.promote(pathway_id)
    assert promoted.json() == {"status": "success", "production_version": 3}
    assert summary(await api.versions(pathway_id))[1] == (3, "published", True, True)

    reverted = await api.revert(pathway_id, version_number=1)
    assert reverted.status_code == 200, reverted.text
    body = reverted.json()
    detail = await assert_one_draft(api, identity, pathway_id)
    v1 = (await api.version(pathway_id, 1)).json()
    assert body["draft_version"] == 4 == detail["version_number"]
    assert body["graph_hash"] == v1["graph_hash"] == detail["graph_hash"]
    assert detail["nodes"] == v1["nodes"]
    assert body["validation"]["valid"] is True
    production = (await api.get(pathway_id, version="production")).json()
    assert production["nodes"][0]["data"]["text"] == "Hello v3"


async def test_get_version_matches_the_version_selector(api: Api) -> None:
    pathway_id = await api.created()
    await api.save(pathway_id)
    by_path = (await api.version(pathway_id, 1)).json()
    by_query = (await api.get(pathway_id, version="1")).json()
    assert by_path == by_query
    assert by_path["version_status"] == "archived"
    assert error_of(await api.version(pathway_id, 9)) == (404, "PATHWAY_VERSION_NOT_FOUND", "version_number")
    assert error_of(await api.version(pathway_id, 0))[:2] == (400, "INVALID_INPUT")
    assert error_of(await api.versions(str(uuid.uuid4())))[:2] == (404, "NOT_FOUND")


async def test_publishing_a_saved_version_forks_nothing_and_keeps_the_first_publish(
    api: Api, identity: IdentityDb
) -> None:
    pathway_id = await api.created()
    await api.save(pathway_id, {"name": "v1"})
    first = await api.publish(pathway_id, version_number=1, environment="staging")
    assert first.json() == {
        "status": "success",
        "published_version": 1,
        "environment": "staging",
        "version_number": 1,
        "draft_version": 2,
        "run_id": None,
    }
    (row,) = [row for row in await version_rows(identity, pathway_id) if row.version_number == 1]
    published_at = row.published_at
    assert published_at is not None
    assert row.published_by is None  # the test key has no creating user

    again = await api.publish(pathway_id, version_number=1, environment="production")
    assert again.json()["draft_version"] == 2
    (row,) = [row for row in await version_rows(identity, pathway_id) if row.version_number == 1]
    assert (row.status, row.published_at) == ("published", published_at)
    assert summary(await api.versions(pathway_id)) == [
        (2, "draft", False, False),
        (1, "published", True, True),
    ]
    rows = await audit_rows(identity, api.org_id, "pathway.publish")
    assert [(row.changes["before"], row.changes["after"]) for row in rows] == [
        ({"environment": "staging", "version": None}, {"environment": "staging", "version": 1}),
        ({"environment": "production", "version": None}, {"environment": "production", "version": 1}),
    ]


async def test_publish_validates_in_publish_mode(api: Api, identity: IdentityDb) -> None:
    pathway_id = await api.created(nodes=[start(prompt="Hi"), start("other", prompt="Yo")], edges=[])
    response = await api.publish(pathway_id)
    assert response.status_code == 400, response.text
    assert {item["details"]["code"] for item in response.json()["errors"]} >= {"PATHWAY_START_NODE"}
    assert summary(await api.versions(pathway_id)) == [(1, "draft", False, False)]
    assert await audit_rows(identity, api.org_id, "pathway.publish") == []
    for call in (api.publish(pathway_id, version_number=7), api.revert(pathway_id, version_number=7)):
        assert error_of(await call) == (404, "PATHWAY_VERSION_NOT_FOUND", "version_number")
    assert error_of(await api.publish(pathway_id, environment="dev"))[:3] == (
        400,
        "INVALID_INPUT",
        "environment",
    )
    assert error_of(await api.revert(pathway_id))[:3] == (400, "INVALID_INPUT", "version_number")
    assert error_of(await api.revert(pathway_id, version_number=0))[:3] == (
        400,
        "INVALID_INPUT",
        "version_number",
    )


async def test_promote_needs_staging_and_audits_changes_only(api: Api, identity: IdentityDb) -> None:
    pathway_id = await api.created()
    assert error_of(await api.promote(pathway_id)) == (409, "CONFLICT", None)
    assert (await api.promote(pathway_id)).json()["errors"][0]["message"] == NO_STAGING_MESSAGE
    await api.publish(pathway_id, environment="staging")
    assert (await api.promote(pathway_id)).json()["production_version"] == 1
    assert (await api.promote(pathway_id)).json()["production_version"] == 1
    (row,) = await audit_rows(identity, api.org_id, "pathway.promote")
    assert (row.changes["before"], row.changes["after"]) == (
        {"production_version": None},
        {"production_version": 1},
    )


async def test_save_and_revert_audits(api: Api, identity: IdentityDb) -> None:
    pathway_id = await api.created(nodes=[start(text="One")], edges=[])
    one = (await api.get(pathway_id)).json()["graph_hash"]
    await api.save(pathway_id, {"name": "first"})
    (saved,) = await audit_rows(identity, api.org_id, "pathway.version_save")
    assert saved.changes["after"] == {"version_number": 1, "name": "first", "graph_hash": one}

    # Reverting to an identical graph and to the draft itself changes nothing.
    assert (await api.revert(pathway_id, version_number=1)).json()["graph_hash"] == one
    assert (await api.revert(pathway_id, version_number=2)).status_code == 200
    assert await audit_rows(identity, api.org_id, "pathway.revert") == []

    two = (await api.update(pathway_id, nodes=[start(text="Two")])).json()["graph_hash"]
    await api.revert(pathway_id, version_number=1)
    (reverted,) = await audit_rows(identity, api.org_id, "pathway.revert")
    assert (reverted.changes["before"], reverted.changes["after"]) == (
        {"graph_hash": two},
        {"graph_hash": one, "version_number": 1},
    )


async def test_revert_reports_save_mode_issues_without_rejecting(api: Api) -> None:
    pathway_id = await api.created(nodes=[start(prompt="Hi"), node("lonely")], edges=[])
    await api.save(pathway_id)
    await api.update(pathway_id, nodes=[start(prompt="Hi")])
    body = (await api.revert(pathway_id, version_number=1)).json()
    assert body["validation"]["valid"] is True
    assert [item["code"] for item in body["validation"]["warnings"]] == ["PATHWAY_UNREACHABLE"]


async def test_publish_and_promote_invalidate_their_cache_keys(api: Api, container: SessionContainer) -> None:
    pathway_id = await api.created()
    production = pathway_cache_key(uuid.UUID(pathway_id), "production")
    staging = pathway_cache_key(uuid.UUID(pathway_id), "staging")
    assert production == f"pathway:{pathway_id}:v:production"
    await container.redis.mset({production: "{}", staging: "{}"})
    await api.publish(pathway_id, environment="staging")
    assert (await container.redis.exists(production), await container.redis.exists(staging)) == (1, 0)
    await api.promote(pathway_id)
    assert await container.redis.exists(production) == 0


async def test_cache_failures_never_fail_a_publish(
    api: Api, container: SessionContainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_keys: str) -> int:
        raise RedisConnectionError("down")

    pathway_id = await api.created()
    stream = json_log_stream()
    monkeypatch.setattr(container.redis, "delete", broken)
    response = await api.publish(pathway_id)
    assert response.status_code == 200, response.text
    (event,) = [item for item in log_events(stream) if item["event"] == "pathway_cache_unavailable"]
    assert (event["pathway_id"], event["environment"]) == (pathway_id, "production")


async def test_concurrent_saves_serialise(api: Api, identity: IdentityDb) -> None:
    pathway_id = await api.created()
    first, second = await asyncio.gather(api.save(pathway_id), api.save(pathway_id))
    assert sorted([first.json()["saved_version"], second.json()["saved_version"]]) == [1, 2]
    await assert_one_draft(api, identity, pathway_id)


async def test_other_orgs_see_no_versions(
    api: Api, client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    pathway_id = await api.created()
    intruder = await star_api(client, identity, container)
    calls = [
        intruder.versions(pathway_id),
        intruder.version(pathway_id, 1),
        intruder.save(pathway_id),
        intruder.publish(pathway_id),
        intruder.promote(pathway_id),
        intruder.revert(pathway_id, version_number=1),
    ]
    for call in calls:
        assert error_of(await call)[:2] == (404, "NOT_FOUND")
    assert summary(await api.versions(pathway_id)) == [(1, "draft", False, False)]
