"""``PathwayRepo`` on a migrated database: create, paging, search, references, reference ids, and org
isolation (specs/12 §1-§3; Decision P07-T09a)."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator
from typing import Any, Final

import pytest
from sms_rows import random_e164
from tenant_rows import OrgFactory, open_database

from va_common.errors import AppError
from va_db.models import (
    Agent,
    AgentVersion,
    KnowledgeBase,
    PathwayFolder,
    Persona,
    PersonaVersion,
    PhoneNumber,
    Tool,
    Widget,
)
from va_db.repositories import NotFoundError, PathwayGraphJson, PathwayReference, PathwayRepo, escape_like
from va_db.session import Database

pytestmark = pytest.mark.integration

GRAPH: Final = PathwayGraphJson(
    nodes=[{"id": "start", "type": "Default", "data": {"name": "Start", "isStart": True, "prompt": "Hi"}}],
    edges=[],
    global_config={},
    graph_hash="0" * 64,
)


@pytest.fixture
async def database(migrated_url: str) -> AsyncIterator[Database]:
    async with open_database(migrated_url) as db:
        yield db


async def create(database: Database, org_id: uuid.UUID, name: str, **values: Any) -> uuid.UUID:
    async with database.tenant_session(org_id) as session:
        pathway, draft = await PathwayRepo(session, org_id).create(
            name=name,
            description=values.get("description", ""),
            folder_id=values.get("folder_id"),
            graph=values.get("graph", GRAPH),
        )
        assert (draft.version_number, draft.status, draft.pathway_id) == (1, "draft", pathway.id)
        return pathway.id


async def add(database: Database, org_id: uuid.UUID, *rows: Any) -> None:
    async with database.tenant_session(org_id) as session:
        for row in rows:
            session.add(row)
            await session.flush()


def test_escape_like_escapes_wildcards_and_the_escape_character() -> None:
    assert escape_like(r"50%_off\now") == r"50\%\_off\\now"
    assert escape_like("plain") == "plain"


async def test_create_get_draft_and_isolation(database: Database) -> None:
    org_id, other = await OrgFactory(database).create(), await OrgFactory(database).create()
    pathway_id = await create(database, org_id, "Orders")
    async with database.tenant_session(org_id) as session:
        repo = PathwayRepo(session, org_id)
        pathway = await repo.get(pathway_id, lock=True)
        draft = await repo.draft(pathway)
        assert (pathway.draft_version, pathway.latest_version, pathway.production_version) == (1, 1, None)
        assert (draft.nodes, draft.graph_hash) == (GRAPH.nodes, GRAPH.graph_hash)
        assert await repo.version(pathway, 2) is None
    async with database.tenant_session(other) as session:
        with pytest.raises(NotFoundError) as caught:
            await PathwayRepo(session, other).get(pathway_id)
    assert caught.value.message == "Pathway not found."


async def test_draft_missing_is_a_data_error(database: Database) -> None:
    org_id = await OrgFactory(database).create()
    pathway_id = await create(database, org_id, "Broken")
    async with database.tenant_session(org_id) as session:
        repo = PathwayRepo(session, org_id)
        pathway = await repo.get(pathway_id)
        pathway.draft_version = 7
        with pytest.raises(LookupError, match="no draft version 7"):
            await repo.draft(pathway)


async def test_page_orders_newest_first_counts_draft_nodes_and_pages_with_cursor(database: Database) -> None:
    org_id = await OrgFactory(database).create()
    two_nodes = PathwayGraphJson(
        nodes=[*GRAPH.nodes, {"id": "b", "type": "Default", "data": {"name": "B", "prompt": "x"}}],
        edges=[],
        global_config={},
        graph_hash="1" * 64,
    )
    ids = [await create(database, org_id, f"P{index}") for index in range(3)]
    ids.append(await create(database, org_id, "P3", graph=two_nodes))
    async with database.tenant_session(org_id) as session:
        repo = PathwayRepo(session, org_id)
        first = await repo.page(limit=3)
        second = await repo.page(limit=3, cursor=first.next_cursor)
    assert [row.pathway.id for row in first.items] == ids[::-1][:3]
    assert [row.node_count for row in first.items] == [2, 1, 1]
    assert first.has_more and first.next_cursor is not None
    assert [row.pathway.id for row in second.items] == [ids[0]]
    assert (second.has_more, second.next_cursor) == (False, None)
    async with database.tenant_session(org_id) as session:
        with pytest.raises(AppError) as caught:
            await PathwayRepo(session, org_id).page(limit=0)
    assert caught.value.field == "limit"


async def test_page_filters_folder_search_and_hides_deleted(database: Database) -> None:
    org_id = await OrgFactory(database).create()
    folder = PathwayFolder(org_id=org_id, name="Sales")
    await add(database, org_id, folder)
    in_folder = await create(database, org_id, "Sales intake", folder_id=folder.id)
    percent = await create(database, org_id, "100% off")
    await create(database, org_id, "1000 off")
    deleted = await create(database, org_id, "Old sales")
    async with database.tenant_session(org_id) as session:
        repo = PathwayRepo(session, org_id)
        await repo.soft_delete(await repo.get(deleted))
        by_folder = await repo.page(folder_id=folder.id)
        by_search = await repo.page(search="SALES")
        by_percent = await repo.page(search="0%")
        assert await repo.folder_exists(folder.id)
        assert not await repo.folder_exists(uuid.uuid4())
        with pytest.raises(NotFoundError):
            await repo.get(deleted)
    assert [row.pathway.id for row in by_folder.items] == [in_folder]
    assert [row.pathway.id for row in by_search.items] == [in_folder]
    assert [row.pathway.id for row in by_percent.items] == [percent]


async def test_update_draft_applies_values_and_graph_and_moves_updated_at(database: Database) -> None:
    org_id = await OrgFactory(database).create()
    pathway_id = await create(database, org_id, "Orders")
    changed = PathwayGraphJson(nodes=[], edges=[], global_config={"globalPrompt": "x"}, graph_hash="2" * 64)
    async with database.tenant_session(org_id) as session:
        repo = PathwayRepo(session, org_id)
        pathway = await repo.get(pathway_id, lock=True)
        created_at = pathway.updated_at
        draft = await repo.draft(pathway)
        await repo.update_draft(pathway, draft, values={"name": "Orders v2"}, graph=changed)
    async with database.tenant_session(org_id) as session:
        repo = PathwayRepo(session, org_id)
        stored = await repo.get(pathway_id)
        draft = await repo.draft(stored)
    assert (stored.name, draft.global_config, draft.graph_hash) == (
        "Orders v2",
        {"globalPrompt": "x"},
        "2" * 64,
    )
    assert stored.updated_at > created_at


async def test_references_find_every_live_kind_and_skip_dead_ones(database: Database) -> None:
    org_id = await OrgFactory(database).create()
    pathway_id = await create(database, org_id, "Orders")
    other_pathway = await create(database, org_id, "Other")
    live_number = PhoneNumber(
        org_id=org_id, e164=random_e164(), country_code="US", inbound_config={"pathway_id": str(pathway_id)}
    )
    released = PhoneNumber(
        org_id=org_id,
        e164=random_e164(),
        country_code="US",
        status="released",
        inbound_config={"pathway_id": str(pathway_id)},
    )
    unrelated = PhoneNumber(
        org_id=org_id,
        e164=random_e164(),
        country_code="US",
        inbound_config={"pathway_id": str(other_pathway)},
    )
    pinned = Agent(org_id=org_id, name="Front desk", env_pins={"production": "1.0.0", "notes": 3})
    unpinned = Agent(org_id=org_id, name="Drafty", env_pins={"production": "2.0.0"})
    listless = Agent(org_id=org_id, name="Odd pins", env_pins={})
    persona = Persona(org_id=org_id, name="Ava", production_version=1)
    draft_persona = Persona(org_id=org_id, name="Draft only", production_version=None)
    widget = Widget(
        org_id=org_id, name="Site chat", pathway_id=pathway_id, public_key=f"wgt_{secrets.token_hex(8)}"
    )
    await add(database, org_id, live_number, released, unrelated, pinned, unpinned, listless, persona)
    await add(database, org_id, draft_persona, widget)
    await add(
        database,
        org_id,
        AgentVersion(org_id=org_id, agent_id=pinned.id, semver="1.0.0", pathway_id=pathway_id),
        AgentVersion(org_id=org_id, agent_id=pinned.id, semver="1.1.0", pathway_id=pathway_id),
        AgentVersion(org_id=org_id, agent_id=unpinned.id, semver="1.0.0", pathway_id=pathway_id),
        AgentVersion(org_id=org_id, agent_id=listless.id, semver="1.0.0", pathway_id=pathway_id),
        PersonaVersion(
            org_id=org_id,
            persona_id=persona.id,
            version_number=1,
            status="published",
            config={"pathway_id": str(pathway_id)},
        ),
        PersonaVersion(
            org_id=org_id,
            persona_id=draft_persona.id,
            version_number=1,
            status="draft",
            config={"pathway_id": str(pathway_id)},
        ),
    )
    async with database.tenant_session(org_id) as session:
        references = await PathwayRepo(session, org_id).references(pathway_id)
        none = await PathwayRepo(session, org_id).references(other_pathway)
    assert references == [
        PathwayReference("phone_number", live_number.id, live_number.e164),
        PathwayReference("agent", pinned.id, "Front desk"),
        PathwayReference("persona", persona.id, "Ava"),
        PathwayReference("widget", widget.id, "Site chat"),
    ]
    assert none == [PathwayReference("phone_number", unrelated.id, unrelated.e164)]


async def test_references_ignore_soft_deleted_owners(database: Database) -> None:
    org_id = await OrgFactory(database).create()
    pathway_id = await create(database, org_id, "Orders")
    widget = Widget(
        org_id=org_id, name="Gone", pathway_id=pathway_id, public_key=f"wgt_{secrets.token_hex(8)}"
    )
    await add(database, org_id, widget)
    async with database.tenant_session(org_id) as session:
        row = await session.get(Widget, widget.id)
        assert row is not None
        row.deleted_at = row.created_at
    async with database.tenant_session(org_id) as session:
        assert await PathwayRepo(session, org_id).references(pathway_id) == []


async def test_ref_ids_hold_live_kbs_tools_and_pathways_of_the_org(database: Database) -> None:
    org_id, other = await OrgFactory(database).create(), await OrgFactory(database).create()
    tool = Tool(org_id=org_id, name="hours", description="Opening hours", url="https://hours.test")
    dead_tool = Tool(org_id=org_id, name="old", description="Old", url="https://old.test")
    kb = KnowledgeBase(org_id=org_id, name="Menu", embedding_model="m", embedding_dimensions=1024)
    await add(database, org_id, tool, dead_tool, kb)
    foreign = Tool(org_id=other, name="theirs", description="Theirs", url="https://theirs.test")
    await add(database, other, foreign)
    pathway_id = await create(database, org_id, "Orders")
    deleted = await create(database, org_id, "Deleted")
    async with database.tenant_session(org_id) as session:
        repo = PathwayRepo(session, org_id)
        row = await session.get(Tool, dead_tool.id)
        assert row is not None
        row.deleted_at = row.created_at
        await repo.soft_delete(await repo.get(deleted))
        ids = await repo.ref_ids()
    assert ids.tool_ids == {tool.id}
    assert ids.kb_ids == {kb.id}
    assert ids.pathway_ids == {pathway_id}


async def test_versions_freeze_fork_and_pin(database: Database) -> None:
    org_id, other = await OrgFactory(database).create(), await OrgFactory(database).create()
    pathway_id = await create(database, org_id, "Versions")
    user_id = uuid.uuid4()
    async with database.tenant_session(org_id) as session:
        repo = PathwayRepo(session, org_id)
        pathway = await repo.get(pathway_id, lock=True)
        draft = await repo.draft(pathway)
        await repo.freeze(draft, status="archived", name="first")
        second = await repo.fork_draft(pathway, draft)
        assert (second.version_number, second.status, second.name) == (2, "draft", None)
        assert PathwayGraphJson.of(second) == PathwayGraphJson.of(draft) == GRAPH
        assert (pathway.draft_version, pathway.latest_version) == (2, 2)
        assert (draft.status, draft.name, draft.published_at) == ("archived", "first", None)

        await repo.freeze(second, status="published")
        first_publish = second.published_at
        assert first_publish is not None
        assert second.published_by is None
        await repo.freeze(second, status="published", published_by=user_id)
        assert (second.published_at, second.published_by) == (first_publish, None)

        await repo.set_pin(pathway, "staging", 2)
        await repo.set_pin(pathway, "production", 1)
        assert (pathway.production_version, pathway.staging_version) == (1, 2)
        assert [version.version_number for version in await repo.versions(pathway)] == [2, 1]
    async with database.tenant_session(other) as session:
        with pytest.raises(NotFoundError):
            await PathwayRepo(session, other).get(pathway_id)
