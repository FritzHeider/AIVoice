"""Pathways API core: create, list, get, update draft, delete with references, validate (specs/12 §1-§3, §11;
Decision P07-T09a). Roles per specs/12 §10 live in ``test_pathways_permissions.py``."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Final

import httpx
import pytest
from graph_helpers import edge, fixture, node, start
from identity_db import IdentityDb
from session_helpers import SessionContainer, audit_rows, error_of, make_member, store_api_key
from sms_rows import random_e164
from sqlalchemy import update

from va_api.services.pathways import DEFAULT_START_NODE
from va_common.ids import PublicPrefix, new_id, to_public
from va_db.models import Pathway, PathwayFolder, PhoneNumber, Tool, Widget
from va_engine.models import graph_hash, load_graph

pytestmark = pytest.mark.integration

BASE: Final = "/v1/pathway"


class Api:
    """An org's ``*`` API key on the shared client."""

    def __init__(self, client: httpx.AsyncClient, org_id: uuid.UUID, key: str) -> None:
        self.client = client
        self.org_id = org_id
        self.headers = {"Authorization": key}

    async def create(self, **body: Any) -> httpx.Response:
        return await self.client.post(
            f"{BASE}/create", json={"name": "Bella Pasta", **body}, headers=self.headers
        )

    async def created(self, **body: Any) -> str:
        response = await self.create(**body)
        assert response.status_code == 200, response.text
        assert response.json()["version_number"] == 1
        return str(response.json()["pathway_id"])

    async def get(self, pathway_id: str, **params: str) -> httpx.Response:
        return await self.client.get(f"{BASE}/{pathway_id}", params=params, headers=self.headers)

    async def list(self, **params: str) -> httpx.Response:
        return await self.client.get(BASE, params=params, headers=self.headers)

    async def update(self, pathway_id: str, **body: Any) -> httpx.Response:
        return await self.client.post(f"{BASE}/{pathway_id}", json=body, headers=self.headers)

    async def delete(self, pathway_id: str, **params: str) -> httpx.Response:
        return await self.client.delete(f"{BASE}/{pathway_id}", params=params, headers=self.headers)

    async def validate(self, pathway_id: str, body: dict[str, Any] | None = None) -> httpx.Response:
        return await self.client.post(f"{BASE}/{pathway_id}/validate", json=body, headers=self.headers)


@pytest.fixture
async def api(client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer) -> Api:
    member = await make_member(identity, container.password_hasher)
    key, _ = await store_api_key(container, member.org.id)
    return Api(client, member.org.id, key)


async def add(identity: IdentityDb, org_id: uuid.UUID, *rows: Any) -> None:
    async with identity.database.tenant_session(org_id) as session:
        for row in rows:
            session.add(row)
            await session.flush()


def codes(response: httpx.Response, kind: str = "warnings") -> list[str]:
    return [item["code"] for item in response.json()["validation"][kind]]


# ---- create / get ------------------------------------------------------------------------------------------


async def test_create_without_graph_has_the_default_start_node(api: Api, identity: IdentityDb) -> None:
    pathway_id = await api.created(
        description="Takes phone orders", global_config={"globalPrompt": "You are Ava."}
    )
    response = await api.get(pathway_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["nodes"] == [DEFAULT_START_NODE]
    assert (body["edges"], body["global_config"]) == ([], {"globalPrompt": "You are Ava."})
    expected_hash = graph_hash({"nodes": body["nodes"], "edges": [], "global_config": body["global_config"]})
    assert body["graph_hash"] == expected_hash
    assert {key: body[key] for key in ("status", "name", "description", "folder_id")} == {
        "status": "success",
        "name": "Bella Pasta",
        "description": "Takes phone orders",
        "folder_id": None,
    }
    assert (body["version_number"], body["version_status"]) == (1, "draft")
    assert (body["production_version"], body["staging_version"]) == (None, None)
    (row,) = await audit_rows(identity, api.org_id, "pathway.create")
    assert (row.resource_type, row.resource_id) == ("pathway", pathway_id)
    assert row.changes["after"] == {
        "name": "Bella Pasta",
        "description": "Takes phone orders",
        "folder_id": None,
        "graph_hash": expected_hash,
    }


async def test_create_stores_a_bland_graph_losslessly_and_reports_missing_refs(api: Api) -> None:
    raw = fixture("restaurant_ordering")
    pathway_id = await api.created(**raw)
    body = (await api.get(pathway_id)).json()
    assert body["graph_hash"] == load_graph(raw["nodes"], raw["edges"], raw["global_config"]).hash()
    assert [item["id"] for item in body["nodes"]] == [item["id"] for item in raw["nodes"]]
    response = await api.update(pathway_id)
    assert response.status_code == 200, response.text
    assert response.json()["validation"]["valid"] is True
    assert {"PATHWAY_KB_MISSING", "PATHWAY_TOOL_MISSING"} <= set(codes(response))


async def test_existing_tools_and_kb_ids_satisfy_reference_checks(api: Api, identity: IdentityDb) -> None:
    tool = Tool(org_id=api.org_id, name="hours", description="Opening hours", url="https://hours.test")
    await add(identity, api.org_id, tool)
    known = start(prompt="Hi", tools=[to_public(PublicPrefix.TOOL, tool.id), str(tool.id)])
    pathway_id = await api.created(nodes=[known], edges=[])
    assert codes(await api.update(pathway_id)) == []
    unknown = start(prompt="Hi", tools=[to_public(PublicPrefix.TOOL, new_id())])
    assert codes(await api.update(pathway_id, nodes=[unknown])) == ["PATHWAY_TOOL_MISSING"]


async def test_create_rejects_bad_names_schema_problems_and_unknown_folders(api: Api) -> None:
    assert error_of(await api.create(name="   "))[:3] == (400, "INVALID_INPUT", "name")
    assert error_of(await api.create(name="x" * 201))[:3] == (400, "INVALID_INPUT", "name")
    schema = await api.create(nodes=[{"id": "a", "type": "Default", "data": {}}], edges=[])
    assert error_of(schema) == (400, "INVALID_INPUT", "nodes[0].data.name")
    folder = await api.create(folder_id=str(uuid.uuid4()))
    assert error_of(folder) == (404, "NOT_FOUND", "folder_id")
    assert (await api.list()).json()["pathways"] == []


async def test_save_mode_errors_reject_with_one_item_per_issue(api: Api) -> None:
    nodes = [start(prompt="Hi"), node("a"), node("a")]
    response = await api.create(nodes=nodes, edges=[edge("e1", "start", "ghost")])
    assert response.status_code == 400, response.text
    body = response.json()
    assert [item["details"]["code"] for item in body["errors"]] == [
        "PATHWAY_DUPLICATE_NODE_ID",
        "PATHWAY_EDGE_DANGLING",
    ]
    assert {item["error"] for item in body["errors"]} == {"INVALID_INPUT"}
    assert body["message"] == body["errors"][0]["message"]
    assert body["errors"][0]["details"]["node_id"] == "a"
    assert body["errors"][1]["details"]["edge_id"] == "e1"
    # Publish-only problems (two start nodes) are warnings in save mode.
    two_starts = await api.created(nodes=[start(prompt="Hi"), start("other", prompt="Yo")], edges=[])
    assert "PATHWAY_START_NODE" in codes(await api.update(two_starts))


async def test_get_selects_versions(api: Api, identity: IdentityDb) -> None:
    pathway_id = await api.created()
    assert (await api.get(pathway_id, version="1")).json()["version_number"] == 1
    assert (await api.get(pathway_id, version="draft")).json()["version_status"] == "draft"
    for version in ("production", "staging", "2"):
        assert error_of(await api.get(pathway_id, version=version))[:2] == (404, "PATHWAY_VERSION_NOT_FOUND")
    for version in ("abc", "0", "-1", "1.5"):
        assert error_of(await api.get(pathway_id, version=version)) == (400, "INVALID_INPUT", "version")
    async with identity.database.system_session() as session:
        await session.execute(
            update(Pathway).where(Pathway.id == uuid.UUID(pathway_id)).values(production_version=1)
        )
    production = (await api.get(pathway_id, version="production")).json()
    assert (production["version_number"], production["production_version"]) == (1, 1)
    assert error_of(await api.get(str(new_id())))[:2] == (404, "NOT_FOUND")


# ---- list --------------------------------------------------------------------------------------------------


async def test_list_pages_filters_and_counts_draft_nodes(api: Api, identity: IdentityDb) -> None:
    folder = PathwayFolder(org_id=api.org_id, name="Sales")
    await add(identity, api.org_id, folder)
    first = await api.created(name="Sales intake", folder_id=str(folder.id))
    second = await api.created(name="100% off", nodes=[start(prompt="Hi"), node("b")], edges=[])
    third = await api.created(name="1000 off")
    page = (await api.list(limit="2")).json()
    assert [item["pathway_id"] for item in page["pathways"]] == [third, second]
    assert page["pathways"][1]["node_count"] == 2
    item = page["pathways"][0]
    assert {key: item[key] for key in ("draft_version", "latest_version", "production_version")} == {
        "draft_version": 1,
        "latest_version": 1,
        "production_version": None,
    }
    rest = (await api.list(limit="2", cursor=page["next_cursor"])).json()
    assert ([item["pathway_id"] for item in rest["pathways"]], rest["next_cursor"]) == ([first], None)
    assert [item["pathway_id"] for item in (await api.list(search="0%")).json()["pathways"]] == [second]
    assert [item["pathway_id"] for item in (await api.list(search="SALES")).json()["pathways"]] == [first]
    by_folder = (await api.list(folder_id=str(folder.id))).json()["pathways"]
    assert [item["pathway_id"] for item in by_folder] == [first]
    assert error_of(await api.list(limit="0"))[:3] == (400, "INVALID_INPUT", "limit")
    assert error_of(await api.list(limit="101"))[:3] == (400, "INVALID_INPUT", "limit")
    assert error_of(await api.list(cursor="nope"))[:3] == (400, "INVALID_INPUT", "cursor")


# ---- update ------------------------------------------------------------------------------------------------


async def test_update_replaces_sent_parts_and_audits_real_changes(api: Api, identity: IdentityDb) -> None:
    pathway_id = await api.created(global_config={"globalPrompt": "You are Ava."})
    before = (await api.get(pathway_id)).json()
    nodes = [start(prompt="Welcome!"), node("menu")]
    response = await api.update(
        pathway_id,
        name="Bella Pasta v2",
        nodes=nodes,
        edges=[edge("e1", "start", "menu", "Menu")],
        expected_graph_hash=before["graph_hash"],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    after = (await api.get(pathway_id)).json()
    assert (after["name"], after["nodes"], after["global_config"]) == (
        "Bella Pasta v2",
        nodes,
        {"globalPrompt": "You are Ava."},
    )
    assert (body["version_number"], body["graph_hash"]) == (1, after["graph_hash"])
    assert body["validation"] == {"valid": True, "errors": [], "warnings": []}
    (row,) = await audit_rows(identity, api.org_id, "pathway.update")
    assert row.changes == {
        "before": {"name": "Bella Pasta", "graph_hash": before["graph_hash"]},
        "after": {"name": "Bella Pasta v2", "graph_hash": after["graph_hash"]},
    }
    assert (await api.update(pathway_id, name="Bella Pasta v2")).status_code == 200
    assert len(await audit_rows(identity, api.org_id, "pathway.update")) == 1


async def test_update_moves_folders_only_when_folder_id_is_sent(api: Api, identity: IdentityDb) -> None:
    folder = PathwayFolder(org_id=api.org_id, name="Sales")
    await add(identity, api.org_id, folder)
    pathway_id = await api.created(folder_id=str(folder.id))
    assert (await api.update(pathway_id, description="Orders")).status_code == 200
    assert (await api.get(pathway_id)).json()["folder_id"] == str(folder.id)
    assert (await api.update(pathway_id, folder_id=None)).status_code == 200
    assert (await api.get(pathway_id)).json()["folder_id"] is None
    assert error_of(await api.update(pathway_id, folder_id=str(uuid.uuid4()))) == (
        404,
        "NOT_FOUND",
        "folder_id",
    )


async def test_update_rejects_a_stale_hash_and_invalid_graphs(api: Api) -> None:
    pathway_id = await api.created()
    current = (await api.get(pathway_id)).json()["graph_hash"]
    stale = await api.update(pathway_id, name="New", expected_graph_hash="0" * 64)
    assert error_of(stale) == (409, "CONFLICT", "expected_graph_hash")
    assert stale.json()["errors"][0]["details"] == {"current_graph_hash": current}
    broken = await api.update(pathway_id, edges=[edge("e1", "start", "ghost")])
    assert broken.status_code == 400
    assert broken.json()["errors"][0]["details"]["code"] == "PATHWAY_EDGE_DANGLING"
    assert error_of(await api.update(pathway_id, edges=[{"id": "e"}])) == (
        400,
        "INVALID_INPUT",
        "edges[0].source",
    )
    unchanged = (await api.get(pathway_id)).json()
    assert (unchanged["name"], unchanged["graph_hash"]) == ("Bella Pasta", current)
    assert error_of(await api.update(str(new_id()), name="x"))[:2] == (404, "NOT_FOUND")


async def test_concurrent_updates_with_the_same_hash_serialise(api: Api) -> None:
    pathway_id = await api.created()
    loaded = (await api.get(pathway_id)).json()["graph_hash"]
    responses = await asyncio.gather(
        api.update(pathway_id, nodes=[start(prompt="One")], expected_graph_hash=loaded),
        api.update(pathway_id, nodes=[start(prompt="Two")], expected_graph_hash=loaded),
    )
    assert sorted(response.status_code for response in responses) == [200, 409]


# ---- validate ----------------------------------------------------------------------------------------------


async def test_validate_reports_issues_without_failing(api: Api) -> None:
    pathway_id = await api.created()
    draft = await api.validate(pathway_id)
    assert draft.status_code == 200, draft.text
    assert draft.json() == {"status": "success", "validation": {"valid": True, "errors": [], "warnings": []}}
    two_starts = {"nodes": [start(prompt="Hi"), start("b", prompt="Yo")], "edges": []}
    published = await api.validate(pathway_id, two_starts)
    assert codes(published, "errors") == ["PATHWAY_START_NODE"]
    assert published.json()["validation"]["valid"] is False
    saved = await api.validate(pathway_id, {**two_starts, "mode": "save"})
    assert (codes(saved, "errors"), codes(saved)) == ([], ["PATHWAY_START_NODE"])
    edges_only = await api.validate(pathway_id, {"edges": [edge("e1", "start", "ghost")], "mode": "save"})
    assert codes(edges_only, "errors") == ["PATHWAY_EDGE_DANGLING"]
    assert error_of(await api.validate(pathway_id, {"nodes": [{"id": "x"}]}))[:2] == (400, "INVALID_INPUT")
    # Validating never changes the draft.
    assert (await api.get(pathway_id)).json()["nodes"] == [DEFAULT_START_NODE]


# ---- delete ------------------------------------------------------------------------------------------------


async def test_delete_refuses_referenced_pathways_unless_forced(api: Api, identity: IdentityDb) -> None:
    pathway_id = await api.created()
    number = PhoneNumber(
        org_id=api.org_id, e164=random_e164(), country_code="US", inbound_config={"pathway_id": pathway_id}
    )
    widget = Widget(
        org_id=api.org_id,
        name="Site chat",
        pathway_id=uuid.UUID(pathway_id),
        public_key=f"wgt_{new_id().hex}",
    )
    await add(identity, api.org_id, number, widget)
    refused = await api.delete(pathway_id)
    assert error_of(refused)[:2] == (409, "CONFLICT")
    assert refused.json()["errors"][0]["details"]["references"] == [
        {"type": "phone_number", "id": str(number.id), "name": number.e164},
        {"type": "widget", "id": str(widget.id), "name": "Site chat"},
    ]
    assert (await api.get(pathway_id)).status_code == 200
    forced = await api.delete(pathway_id, force="true")
    assert forced.status_code == 200, forced.text
    assert forced.json() == {"status": "success", "message": "Pathway deleted."}
    (row,) = await audit_rows(identity, api.org_id, "pathway.delete")
    assert (row.resource_id, row.changes["after"]) == (pathway_id, {"force": True, "references": 2})
    assert error_of(await api.get(pathway_id))[:2] == (404, "NOT_FOUND")
    assert error_of(await api.update(pathway_id, name="x"))[:2] == (404, "NOT_FOUND")
    assert error_of(await api.validate(pathway_id))[:2] == (404, "NOT_FOUND")
    assert error_of(await api.delete(pathway_id))[:2] == (404, "NOT_FOUND")
    assert (await api.list()).json()["pathways"] == []


async def test_delete_unreferenced_and_other_orgs_see_nothing(
    api: Api, client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    pathway_id = await api.created()
    other = await make_member(identity, container.password_hasher)
    key, _ = await store_api_key(container, other.org.id)
    stranger = Api(client, other.org.id, key)
    assert error_of(await stranger.get(pathway_id))[:2] == (404, "NOT_FOUND")
    assert error_of(await stranger.update(pathway_id, name="Mine"))[:2] == (404, "NOT_FOUND")
    assert error_of(await stranger.validate(pathway_id))[:2] == (404, "NOT_FOUND")
    assert error_of(await stranger.delete(pathway_id, force="true"))[:2] == (404, "NOT_FOUND")
    assert (await stranger.list()).json()["pathways"] == []
    deleted = await api.delete(pathway_id)
    assert deleted.status_code == 200, deleted.text
    (row,) = await audit_rows(identity, api.org_id, "pathway.delete")
    assert row.changes["after"] == {"force": False, "references": 0}


async def test_force_delete_needs_a_star_key(api: Api, container: SessionContainer) -> None:
    pathway_id = await api.created()
    narrow, _ = await store_api_key(container, api.org_id, ("pathways:read", "pathways:write"))
    limited = Api(api.client, api.org_id, narrow)
    assert error_of(await limited.delete(pathway_id, force="true"))[:2] == (403, "FORBIDDEN")
    assert (await limited.delete(pathway_id)).status_code == 200
