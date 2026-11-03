"""Pathway import, export, and duplicate (specs/12 §4 duplicate, §5; Decision P07-T09c). Roles live in
``test_pathways_permissions.py``."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from graph_helpers import edge, fixture, node, start
from identity_db import IdentityDb
from pathway_helpers import Api, star_api
from session_helpers import SessionContainer, audit_rows, error_of

from va_api.services.pathway_io import EXPORT_FORMAT

pytestmark = pytest.mark.integration

FIXTURES = ("appointment_booking", "lead_qualification", "legacy_import", "restaurant_ordering")
EXPORT_KEYS = {"format", "name", "description", "nodes", "edges", "global_config", "exported_at"}


@pytest.fixture
async def api(client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer) -> Api:
    return await star_api(client, identity, container)


def ok(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


@pytest.mark.parametrize("name", FIXTURES)
async def test_bland_fixtures_import_and_round_trip(name: str, api: Api) -> None:
    raw = fixture(name)
    imported = ok(await api.import_({"name": name, **raw}))
    assert imported["version_number"] == 1
    assert imported["validation"]["valid"] is True
    missing = {"PATHWAY_TOOL_MISSING", "PATHWAY_KB_MISSING"}
    assert imported["warnings"] == [
        item for item in imported["validation"]["warnings"] if item["code"] in missing
    ]
    assert imported["warnings"]

    pathway_id = imported["pathway_id"]
    detail = ok(await api.get(pathway_id))
    exported = ok(await api.export(pathway_id))
    assert set(exported) == EXPORT_KEYS
    assert exported["format"] == EXPORT_FORMAT
    assert (exported["nodes"], exported["edges"], exported["global_config"]) == (
        detail["nodes"],
        detail["edges"],
        detail["global_config"],
    )

    again = ok(await api.import_(exported))
    assert again["warnings"] == imported["warnings"]
    copy = ok(await api.get(again["pathway_id"]))
    assert copy["graph_hash"] == detail["graph_hash"]
    assert (copy["name"], copy["nodes"], copy["edges"]) == (name, detail["nodes"], detail["edges"])


async def test_legacy_aliases_are_normalised(api: Api, identity: IdentityDb) -> None:
    raw = fixture("legacy_import")
    imported = ok(await api.import_({"nodes": raw["nodes"], "edges": raw["edges"], "exported_at": "x"}))
    detail = ok(await api.get(imported["pathway_id"]))
    assert detail["name"] == "Imported pathway"
    types = {item["id"]: item["type"] for item in detail["nodes"]}
    assert "Transfer Node" not in types.values()
    assert types["3"] == "Transfer Call"
    assert [item["code"] for item in imported["warnings"]] == ["PATHWAY_KB_MISSING"]
    (row,) = await audit_rows(identity, api.org_id, "pathway.import")
    assert row.resource_id == imported["pathway_id"]
    assert row.changes["after"]["format"] == "bland"


async def test_bland_global_config_alias_and_our_format(api: Api, identity: IdentityDb) -> None:
    config = {"globalPrompt": "You are Ava."}
    bland = ok(await api.import_({"nodes": [start(prompt="Hi")], "edges": [], "globalConfig": config}))
    assert ok(await api.get(bland["pathway_id"]))["global_config"] == config
    ours = ok(
        await api.import_(
            {"format": EXPORT_FORMAT, "name": "Ours", "nodes": [start(prompt="Hi")], "edges": []}
        )
    )
    rows = await audit_rows(identity, api.org_id, "pathway.import")
    assert [row.changes["after"]["format"] for row in rows] == ["bland", EXPORT_FORMAT]
    assert ok(await api.get(ours["pathway_id"]))["name"] == "Ours"


async def test_import_rejects_bad_formats_and_invalid_graphs(api: Api, identity: IdentityDb) -> None:
    bad_format = await api.import_({"format": "other.v9", "nodes": [start(prompt="Hi")], "edges": []})
    assert error_of(bad_format) == (400, "INVALID_INPUT", "format")
    dangling = await api.import_(
        {"nodes": [start(prompt="Hi")], "edges": [edge("e1", "start", "nowhere", "Go on")]}
    )
    assert dangling.status_code == 400, dangling.text
    assert all(item["error"] == "INVALID_INPUT" for item in dangling.json()["errors"])
    assert error_of(await api.import_({"edges": []}))[:3] == (400, "INVALID_INPUT", "nodes")
    assert (await api.list()).json()["pathways"] == []
    assert await audit_rows(identity, api.org_id, "pathway.import") == []


async def test_export_selects_versions(api: Api, container: SessionContainer) -> None:
    pathway_id = await api.created(nodes=[start(text="Hello v1")], edges=[], description="Orders")
    assert error_of(await api.export(pathway_id, version="production")) == (
        404,
        "PATHWAY_VERSION_NOT_FOUND",
        "version",
    )
    assert error_of(await api.export(pathway_id, version="zero"))[:3] == (400, "INVALID_INPUT", "version")
    ok(await api.publish(pathway_id))
    await api.update(pathway_id, nodes=[start(text="Hello v2")])
    production = ok(await api.export(pathway_id, version="production"))
    draft = ok(await api.export(pathway_id))
    assert production["nodes"][0]["data"]["text"] == "Hello v1"
    assert draft["nodes"][0]["data"]["text"] == "Hello v2"
    assert ok(await api.export(pathway_id, version="1"))["nodes"] == production["nodes"]
    assert draft["description"] == "Orders"
    assert draft["exported_at"] == container.clock.now().isoformat().replace("+00:00", "Z")


async def test_duplicate_copies_the_chosen_version(api: Api, identity: IdentityDb) -> None:
    folder_id = await api.folder_created("Restaurants")
    pathway_id = await api.created(
        nodes=[start(text="Hello v1")], edges=[], description="Orders", folder_id=folder_id
    )
    ok(await api.publish(pathway_id))
    await api.update(pathway_id, nodes=[start(text="Hello v2")])

    default = ok(await api.duplicate(pathway_id))
    assert default["version_number"] == 1
    copy = ok(await api.get(default["pathway_id"]))
    assert (copy["name"], copy["description"], copy["folder_id"]) == (
        "Copy of Bella Pasta",
        "Orders",
        folder_id,
    )
    assert copy["nodes"][0]["data"]["text"] == "Hello v2"
    assert (copy["production_version"], copy["version_status"]) == (None, "draft")

    named = ok(await api.duplicate(pathway_id, {"name": " Night menu ", "version": "production"}))
    night = ok(await api.get(named["pathway_id"]))
    assert (night["name"], night["nodes"][0]["data"]["text"]) == ("Night menu", "Hello v1")
    numbered = ok(await api.duplicate(pathway_id, {"version": 1}))
    assert ok(await api.get(numbered["pathway_id"]))["nodes"][0]["data"]["text"] == "Hello v1"

    for missing in ({"version": 9}, {"version": "staging"}):
        assert error_of(await api.duplicate(pathway_id, missing)) == (
            404,
            "PATHWAY_VERSION_NOT_FOUND",
            "version",
        )
    assert error_of(await api.duplicate(pathway_id, {"version": 0}))[:2] == (400, "INVALID_INPUT")

    rows = await audit_rows(identity, api.org_id, "pathway.duplicate")
    assert [row.resource_id for row in rows] == [
        default["pathway_id"],
        named["pathway_id"],
        numbered["pathway_id"],
    ]
    assert rows[1].changes["after"]["source_pathway_id"] == pathway_id
    assert [row.changes["after"]["source_version"] for row in rows] == [2, 1, 1]


async def test_duplicate_name_is_cut_to_200_characters(api: Api) -> None:
    pathway_id = await api.created(name="N" * 200)
    duplicated = ok(await api.duplicate(pathway_id))
    name = ok(await api.get(duplicated["pathway_id"]))["name"]
    assert name == ("Copy of " + "N" * 200)[:200]
    assert len(name) == 200


async def test_duplicate_and_export_are_tenant_scoped(
    api: Api, client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    other = await star_api(client, identity, container)
    foreign = await other.created(nodes=[start(prompt="Hi"), node("next", "Next")], edges=[])
    assert error_of(await api.duplicate(foreign)) == (404, "NOT_FOUND", None)
    assert error_of(await api.export(foreign)) == (404, "NOT_FOUND", None)


async def test_import_route_is_not_taken_by_pathway_ids(api: Api) -> None:
    response = await api.import_({"nodes": [start(prompt="Hi")], "edges": []})
    assert ok(response)["version_number"] == 1
