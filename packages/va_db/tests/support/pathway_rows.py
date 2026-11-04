"""Shared rows of the pathway repository tests (specs/12-pathways-api.md)."""

from __future__ import annotations

import uuid
from typing import Final

from va_db.repositories import PathwayGraphJson, PathwayRepo
from va_db.session import Database

__all__ = ["GRAPH", "seed_orders_pathway"]

GRAPH: Final = PathwayGraphJson(
    nodes=[{"id": "start", "type": "Default", "data": {"name": "Start", "isStart": True, "prompt": "Hi"}}],
    edges=[],
    global_config={},
    graph_hash="0" * 64,
)


async def seed_orders_pathway(database: Database, org_id: uuid.UUID) -> uuid.UUID:
    """A pathway named ``Orders`` with draft 1 = :data:`GRAPH`; returns its id."""
    async with database.tenant_session(org_id) as session:
        pathway, _ = await PathwayRepo(session, org_id).create(
            name="Orders", description="", folder_id=None, graph=GRAPH
        )
        return pathway.id
