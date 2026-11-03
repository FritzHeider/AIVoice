"""``DbPathwayLoader``: other pathways of the org as engine graphs for the test chat (specs/12 §7 Decision
P07-T10)."""

from __future__ import annotations

import uuid

import pytest
from identity_db import IdentityDb
from pathway_helpers import seed_pathway
from session_helpers import SessionContainer, make_member
from sqlalchemy import update

from va_api.services.pathway_loader import DbPathwayLoader
from va_db.models import PathwayVersion
from va_db.repositories import PathwayRepo
from va_engine.navigation import PathwayUnavailableError

pytestmark = pytest.mark.integration


async def test_loads_pins_and_numbers_and_refuses_everything_else(
    identity: IdentityDb, container: SessionContainer
) -> None:
    member = await make_member(identity, container.password_hasher)
    org_id = member.org.id
    pathway = await seed_pathway(identity, org_id)
    loader = DbPathwayLoader(identity.database, org_id)
    by_number = await loader.load(str(pathway.id), 1)
    assert (by_number.pathway_id, by_number.version) == (pathway.id, 1)
    assert [item.id for item in by_number.graph.nodes] == ["start"]
    reasons = []
    for version in ("production", "staging", 7):
        with pytest.raises(PathwayUnavailableError) as missing:
            await loader.load(str(pathway.id), version)
        reasons.append(missing.value.reason)
    assert reasons == ["version_not_found"] * 3
    async with identity.database.tenant_session(org_id) as session:
        repo = PathwayRepo(session, org_id)
        stored = await repo.get(pathway.id)
        await repo.set_pin(stored, "production", 1)
        await repo.set_pin(stored, "staging", 1)
    assert (await loader.load(str(pathway.id), "production")).version == 1
    assert (await loader.load(str(pathway.id), "staging")).version == 1
    other = await make_member(identity, container.password_hasher)
    refusals = []
    for loader_org, pathway_id in (
        (other.org.id, str(pathway.id)),
        (org_id, str(uuid.uuid4())),
        (org_id, "not-a-uuid"),
    ):
        with pytest.raises(PathwayUnavailableError) as refused:
            await DbPathwayLoader(identity.database, loader_org).load(pathway_id, 1)
        refusals.append(refused.value.reason)
    assert refusals == ["not_found"] * 3
    async with identity.database.tenant_session(org_id) as session:
        await session.execute(
            update(PathwayVersion)
            .where(PathwayVersion.pathway_id == pathway.id)
            .values(nodes=[{"id": "x", "type": "Nope", "data": {}}])
        )
    with pytest.raises(PathwayUnavailableError) as broken:
        await loader.load(str(pathway.id), 1)
    assert broken.value.reason == "invalid_graph"
