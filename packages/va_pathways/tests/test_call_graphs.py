"""``CallGraphCache``: frozen versions cached per process, drafts always read, LRU eviction, pins resolved
every time (specs/11 §10 Decision P07-T12b)."""

from __future__ import annotations

import uuid

import pytest
from pathway_rows import seed_orders_pathway
from sqlalchemy import update
from tenant_rows import OrgFactory, open_database

from va_db.models import PathwayVersion
from va_db.repositories import PathwayRepo
from va_db.session import Database
from va_engine.navigation import ActiveGraph, PathwayUnavailableError
from va_pathways.call_graphs import CallGraphCache

pytestmark = pytest.mark.integration

BROKEN = [{"id": "x", "type": "Nope", "data": {}}]


async def _set_version(
    database: Database, org_id: uuid.UUID, pathway_id: uuid.UUID, **values: object
) -> None:
    async with database.tenant_session(org_id) as session:
        await session.execute(
            update(PathwayVersion).where(PathwayVersion.pathway_id == pathway_id).values(**values)
        )


async def _rename_start(database: Database, org_id: uuid.UUID, pathway_id: uuid.UUID, name: str) -> None:
    async with database.tenant_session(org_id) as session:
        repo = PathwayRepo(session, org_id)
        version = await repo.version(await repo.get(pathway_id), 1)
        assert version is not None
        nodes = [dict(node) for node in version.nodes]
        nodes[0] = {**nodes[0], "data": {**nodes[0]["data"], "name": name}}
        version.nodes = nodes


def _start_name(active: ActiveGraph) -> str | None:
    return active.graph.nodes[0].data.name


async def test_drafts_are_read_every_time(pathways_db_url: str) -> None:
    async with open_database(pathways_db_url) as database:
        org_id = await OrgFactory(database).create()
        pathway_id = await seed_orders_pathway(database, org_id)
        cache = CallGraphCache(database)
        first = await cache.get(org_id, pathway_id, 1)
        await _rename_start(database, org_id, pathway_id, "Renamed")
        assert _start_name(await cache.get(org_id, pathway_id, 1)) == "Renamed"
        assert (first.pathway_id, first.version, len(cache)) == (pathway_id, 1, 0)


async def test_frozen_versions_are_served_from_the_cache(pathways_db_url: str) -> None:
    async with open_database(pathways_db_url) as database:
        org_id = await OrgFactory(database).create()
        pathway_id = await seed_orders_pathway(database, org_id)
        await _set_version(database, org_id, pathway_id, status="published")
        cache = CallGraphCache(database)
        first = await cache.get(org_id, pathway_id, 1)
        await _set_version(database, org_id, pathway_id, nodes=BROKEN)
        assert await cache.get(org_id, pathway_id, 1) is first
        assert len(cache) == 1
        with pytest.raises(PathwayUnavailableError) as other_org:
            await cache.get(await OrgFactory(database).create(), pathway_id, 1)
        assert other_org.value.reason == "not_found"
        with pytest.raises(PathwayUnavailableError) as broken:
            await CallGraphCache(database).get(org_id, pathway_id, 1)
        assert broken.value.reason == "invalid_graph"


async def test_the_least_recently_used_version_is_evicted(pathways_db_url: str) -> None:
    async with open_database(pathways_db_url) as database:
        org_id = await OrgFactory(database).create()
        first_id = await seed_orders_pathway(database, org_id)
        second_id = await seed_orders_pathway(database, org_id)
        for pathway_id in (first_id, second_id):
            await _set_version(database, org_id, pathway_id, status="archived")
        cache = CallGraphCache(database, max_entries=1)
        first = await cache.get(org_id, first_id, 1)
        await cache.get(org_id, second_id, 1)
        assert len(cache) == 1
        assert await cache.get(org_id, first_id, 1) is not first
        with pytest.raises(ValueError, match="max_entries"):
            CallGraphCache(database, max_entries=0)


async def test_the_loader_resolves_pins_and_numbers(pathways_db_url: str) -> None:
    async with open_database(pathways_db_url) as database:
        org_id = await OrgFactory(database).create()
        pathway_id = await seed_orders_pathway(database, org_id)
        cache = CallGraphCache(database)
        loader = cache.loader(org_id)
        with pytest.raises(PathwayUnavailableError) as unpinned:
            await loader.load(str(pathway_id), "production")
        assert unpinned.value.reason == "version_not_found"
        async with database.tenant_session(org_id) as session:
            repo = PathwayRepo(session, org_id)
            await repo.set_pin(await repo.get(pathway_id), "production", 1)
        await _set_version(database, org_id, pathway_id, status="published")
        pinned = await loader.load(str(pathway_id), "production")
        assert (pinned.pathway_id, pinned.version, len(cache)) == (pathway_id, 1, 1)
        assert await loader.load(str(pathway_id), 1) is pinned
        with pytest.raises(PathwayUnavailableError) as not_uuid:
            await loader.load("not-a-uuid", 1)
        assert not_uuid.value.reason == "not_found"
