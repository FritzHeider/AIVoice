"""``ToolRepo`` on a migrated database: create, paging, search, name reuse, conflicts, references, and org
isolation (specs/13 §2; Decision P08-T01)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pathway_rows import GRAPH
from tenant_rows import OrgFactory, open_database

from va_common.errors import AppError
from va_common.ids import PublicPrefix, to_public
from va_db.models import Agent, AgentVersion, PathwayVersion, Persona, PersonaVersion, Tool
from va_db.repositories import PathwayGraphJson, PathwayRepo, ToolReference, ToolRepo
from va_db.session import Database

pytestmark = pytest.mark.integration


@pytest.fixture
async def database(migrated_url: str) -> AsyncIterator[Database]:
    async with open_database(migrated_url) as db:
        yield db


def values(name: str, **overrides: Any) -> dict[str, Any]:
    return {
        "name": name,
        "description": overrides.pop("description", f"Tool {name}"),
        "url": "https://hooks.test/tool",
        **overrides,
    }


async def create(database: Database, org_id: uuid.UUID, name: str, **overrides: Any) -> uuid.UUID:
    async with database.tenant_session(org_id) as session:
        tool = await ToolRepo(session, org_id).create(values(name, **overrides))
        return tool.id


async def add(database: Database, org_id: uuid.UUID, *rows: Any) -> None:
    """Insert ``rows`` in one transaction (parents in an earlier call than their children)."""
    async with database.tenant_session(org_id) as session:
        session.add_all(rows)
        await session.flush()


async def conflict(database: Database, org_id: uuid.UUID, name: str) -> AppError:
    with pytest.raises(AppError) as caught:
        await create(database, org_id, name)
    return caught.value


async def test_create_get_defaults_and_isolation(database: Database) -> None:
    org_id, other = await OrgFactory(database).create(), await OrgFactory(database).create()
    tool_id = await create(database, org_id, "check_availability")
    async with database.tenant_session(org_id) as session:
        tool = await ToolRepo(session, org_id).get(tool_id, lock=True)
        assert (tool.method, tool.timeout_ms, tool.retries, tool.body) == ("POST", 10_000, 0, None)
        assert tool.input_schema == {"type": "object", "properties": {}}
    async with database.tenant_session(other) as session:
        with pytest.raises(AppError) as caught:
            await ToolRepo(session, other).get(tool_id)
    assert (caught.value.code, caught.value.message) == ("TOOL_NOT_FOUND", "Tool not found.")


async def test_create_with_an_explicit_id(database: Database) -> None:
    org_id = await OrgFactory(database).create()
    chosen = uuid.uuid4()
    async with database.tenant_session(org_id) as session:
        tool = await ToolRepo(session, org_id).create(values("chosen"), tool_id=chosen)
    assert tool.id == chosen


async def test_by_ids_keeps_the_order_and_skips_absent_deleted_and_foreign_tools(database: Database) -> None:
    org_id, other = await OrgFactory(database).create(), await OrgFactory(database).create()
    first = await create(database, org_id, "a_tool")
    second = await create(database, org_id, "b_tool")
    gone = await create(database, org_id, "gone")
    foreign = await create(database, other, "theirs")
    async with database.tenant_session(org_id) as session:
        repo = ToolRepo(session, org_id)
        await repo.soft_delete(await repo.get(gone))
    async with database.tenant_session(org_id) as session:
        repo = ToolRepo(session, org_id)
        rows = await repo.by_ids([second, uuid.uuid4(), gone, foreign, first, second])
        assert [row.id for row in rows] == [second, first]
        assert await repo.by_ids([]) == []


async def test_live_names_conflict_and_deleted_names_are_freed(database: Database) -> None:
    org_id = await OrgFactory(database).create()
    first = await create(database, org_id, "lookup_order")
    error = await conflict(database, org_id, "lookup_order")
    assert (error.code, error.field, error.message) == (
        "CONFLICT",
        "name",
        "A tool named 'lookup_order' already exists.",
    )
    async with database.tenant_session(org_id) as session:
        repo = ToolRepo(session, org_id)
        await repo.soft_delete(await repo.get(first))
    second = await create(database, org_id, "lookup_order")
    async with database.tenant_session(org_id) as session:
        ids = set((await session.scalars(Tool.__table__.select().with_only_columns(Tool.id))).all())
        with pytest.raises(AppError):
            await ToolRepo(session, org_id).get(first)
    assert second in ids
    assert first not in ids


async def test_update_renames_and_refuses_a_live_namesake(database: Database) -> None:
    org_id = await OrgFactory(database).create()
    await create(database, org_id, "taken")
    tool_id = await create(database, org_id, "mine")
    async with database.tenant_session(org_id) as session:
        repo = ToolRepo(session, org_id)
        tool = await repo.update(await repo.get(tool_id), {"name": "renamed", "retries": 2})
        assert (tool.name, tool.retries) == ("renamed", 2)
        with pytest.raises(AppError) as caught:
            await repo.update(tool, {"name": "taken"})
        assert (caught.value.code, caught.value.field) == ("CONFLICT", "name")
        assert (await repo.get(tool_id)).name == "renamed"


async def test_update_onto_a_deleted_name_frees_it(database: Database) -> None:
    org_id = await OrgFactory(database).create()
    gone = await create(database, org_id, "old_name")
    tool_id = await create(database, org_id, "new_name")
    async with database.tenant_session(org_id) as session:
        repo = ToolRepo(session, org_id)
        await repo.soft_delete(await repo.get(gone))
        tool = await repo.update(await repo.get(tool_id), {"name": "old_name"})
    assert tool.name == "old_name"


async def test_page_search_escaping_and_cursor(database: Database) -> None:
    org_id = await OrgFactory(database).create()
    names = ["alpha", "beta", "gamma_tool", "delta"]
    for name in names:
        await create(database, org_id, name, description="50% off" if name == "delta" else "plain")
    deleted = await create(database, org_id, "alpha_deleted")
    async with database.tenant_session(org_id) as session:
        repo = ToolRepo(session, org_id)
        await repo.soft_delete(await repo.get(deleted))
        first = await repo.page(limit=3)
        second = await repo.page(limit=3, cursor=first.next_cursor)
        assert [tool.name for tool in first.items] == ["delta", "gamma_tool", "beta"]
        assert [tool.name for tool in second.items] == ["alpha"]
        assert first.next_cursor is not None
        assert second.next_cursor is None
        assert [tool.name for tool in (await repo.page(search="ALPHA")).items] == ["alpha"]
        assert [tool.name for tool in (await repo.page(search="%")).items] == ["delta"]
        assert [tool.name for tool in (await repo.page(search="a_t")).items] == ["gamma_tool"]


async def _published_pathway(database: Database, org_id: uuid.UUID, graph: dict[str, Any]) -> uuid.UUID:
    async with database.tenant_session(org_id) as session:
        pathway, _ = await PathwayRepo(session, org_id).create(
            name="Orders", description="", folder_id=None, graph=GRAPH
        )
        session.add(
            PathwayVersion(
                org_id=org_id,
                pathway_id=pathway.id,
                version_number=2,
                status="published",
                nodes=graph.get("nodes", []),
                edges=[],
                global_config=graph.get("global_config", {}),
                graph_hash="1" * 64,
            )
        )
        await session.flush()
        return pathway.id


async def test_references_cover_every_source_and_both_id_forms(database: Database) -> None:
    org_id = await OrgFactory(database).create()
    tool_id = await create(database, org_id, "referenced")
    public, bare = to_public(PublicPrefix.TOOL, tool_id), str(tool_id)
    node = {"id": "n1", "type": "Default", "data": {"name": "N", "tools": [public]}}
    by_node = await _published_pathway(database, org_id, {"nodes": [node]})
    by_global = await _published_pathway(database, org_id, {"global_config": {"globalTools": [bare]}})
    agent = Agent(org_id=org_id, name="Front desk", env_pins={})
    persona = Persona(org_id=org_id, name="Ava", production_version=1)
    await add(database, org_id, agent, persona)
    await add(
        database,
        org_id,
        AgentVersion(org_id=org_id, agent_id=agent.id, semver="1.2.0", call_settings={"tools": [bare]}),
        PersonaVersion(
            org_id=org_id,
            persona_id=persona.id,
            version_number=1,
            status="published",
            config={"tools": [public]},
        ),
    )
    async with database.tenant_session(org_id) as session:
        references = await ToolRepo(session, org_id).references(tool_id)
    assert set(references) == {
        ToolReference("pathway", by_node, "2"),
        ToolReference("pathway", by_global, "2"),
        ToolReference("persona", persona.id, "1"),
        ToolReference("agent", agent.id, "1.2.0"),
    }


async def test_drafts_deleted_parents_and_other_tools_are_not_references(database: Database) -> None:
    org_id = await OrgFactory(database).create()
    tool_id = await create(database, org_id, "unused")
    other_tool = to_public(PublicPrefix.TOOL, await create(database, org_id, "other"))
    public = to_public(PublicPrefix.TOOL, tool_id)
    node = {"id": "n1", "type": "Default", "data": {"name": "N", "tools": [public]}}
    async with database.tenant_session(org_id) as session:
        # A draft listing the tool is no reference.
        await PathwayRepo(session, org_id).create(
            name="Draft",
            description="",
            folder_id=None,
            graph=PathwayGraphJson(nodes=[node], edges=[], global_config={}, graph_hash="2" * 64),
        )
    deleted_pathway = await _published_pathway(database, org_id, {"nodes": [node]})
    await _published_pathway(database, org_id, {"global_config": {"globalTools": [other_tool]}})
    agent = Agent(org_id=org_id, name="Gone", env_pins={})
    draft_persona = Persona(org_id=org_id, name="Draft persona")
    await add(database, org_id, agent, draft_persona)
    await add(
        database,
        org_id,
        AgentVersion(org_id=org_id, agent_id=agent.id, semver="1.0.0", call_settings={"tools": [public]}),
        PersonaVersion(
            org_id=org_id,
            persona_id=draft_persona.id,
            version_number=1,
            status="draft",
            config={"tools": [public]},
        ),
    )
    async with database.tenant_session(org_id) as session:
        repo = PathwayRepo(session, org_id)
        await repo.soft_delete(await repo.get(deleted_pathway))
        gone = await session.get(Agent, agent.id)
        assert gone is not None
        gone.deleted_at = gone.created_at
        await session.flush()
        assert await ToolRepo(session, org_id).references(tool_id) == []
