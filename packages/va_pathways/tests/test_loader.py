"""``DbPathwayLoader``: other pathways of the org as engine graphs for text runs (specs/12 §7 Decision
P07-T10, §8 Decision P07-T11a)."""

from __future__ import annotations

import uuid

import pytest
from pathway_rows import seed_orders_pathway
from sqlalchemy import update
from tenant_rows import OrgFactory, open_database

from va_db.models import PathwayVersion
from va_db.repositories import PathwayRepo
from va_engine.models.nodes import PathwayVersionRef
from va_engine.navigation import PathwayUnavailableError
from va_pathways.loader import DbPathwayLoader

pytestmark = pytest.mark.integration


async def _reason(loader: DbPathwayLoader, pathway_id: str, version: PathwayVersionRef) -> str:
    with pytest.raises(PathwayUnavailableError) as refused:
        await loader.load(pathway_id, version)
    return refused.value.reason


async def test_loads_pins_and_numbers_and_refuses_everything_else(pathways_db_url: str) -> None:
    async with open_database(pathways_db_url) as database:
        orgs = OrgFactory(database)
        org_id, other_org = await orgs.create(), await orgs.create()
        pathway_id = await seed_orders_pathway(database, org_id)
        loader = DbPathwayLoader(database, org_id)
        by_number = await loader.load(str(pathway_id), 1)
        assert (by_number.pathway_id, by_number.version) == (pathway_id, 1)
        assert [item.id for item in by_number.graph.nodes] == ["start"]
        missing = [
            await _reason(loader, str(pathway_id), version) for version in ("production", "staging", 7)
        ]
        assert missing == ["version_not_found"] * 3
        async with database.tenant_session(org_id) as session:
            repo = PathwayRepo(session, org_id)
            stored = await repo.get(pathway_id)
            await repo.set_pin(stored, "production", 1)
            await repo.set_pin(stored, "staging", 1)
        assert (await loader.load(str(pathway_id), "production")).version == 1
        assert (await loader.load(str(pathway_id), "staging")).version == 1
        refusals = [
            await _reason(DbPathwayLoader(database, other_org), str(pathway_id), 1),
            await _reason(loader, str(uuid.uuid4()), 1),
            await _reason(loader, "not-a-uuid", 1),
        ]
        assert refusals == ["not_found"] * 3
        async with database.tenant_session(org_id) as session:
            await session.execute(
                update(PathwayVersion)
                .where(PathwayVersion.pathway_id == pathway_id)
                .values(nodes=[{"id": "x", "type": "Nope", "data": {}}])
            )
        assert await _reason(loader, str(pathway_id), 1) == "invalid_graph"
