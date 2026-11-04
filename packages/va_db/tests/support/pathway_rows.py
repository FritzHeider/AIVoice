"""Shared rows of the pathway repository tests (specs/12-pathways-api.md)."""

from __future__ import annotations

import uuid
from typing import Any, Final

from va_db.models import PathwayTestRun, PathwayTestRunStatus
from va_db.pathway_publish import publish_record
from va_db.repositories import PathwayGraphJson, PathwayRepo, PathwayRunRepo
from va_db.session import Database
from va_domain.pathway_tests import RegressionPublish

__all__ = ["GRAPH", "locked_run", "publish_of", "seed_orders_pathway", "seed_regression_run"]

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


async def seed_regression_run(
    database: Database,
    org_id: uuid.UUID,
    pathway_id: uuid.UUID,
    intent: RegressionPublish,
    status: PathwayTestRunStatus,
) -> uuid.UUID:
    """A blocking regression run of ``intent``'s version with a ``pending`` publish record, set to ``status``;
    returns its id."""
    record = publish_record(
        block=True,
        environment=intent.environment,
        version_number=intent.version_number,
        graph_hash=intent.graph_hash,
        outcome="pending",
    )
    async with database.tenant_session(org_id) as session:
        run = await PathwayRunRepo(session, org_id).add(
            pathway_id, version_number=intent.version_number, kind="regression", publish=record
        )
        run.status = status
        await session.flush()
        return run.id


async def locked_run(database: Database, org_id: uuid.UUID, run_id: uuid.UUID) -> PathwayTestRun:
    """The run row (it must exist)."""
    async with database.tenant_session(org_id) as session:
        run = await PathwayRunRepo(session, org_id).lock(run_id)
        assert run is not None
        return run


async def publish_of(database: Database, org_id: uuid.UUID, run_id: uuid.UUID) -> dict[str, Any]:
    """The run's publish record (it must have one)."""
    publish = (await locked_run(database, org_id, run_id)).publish
    assert isinstance(publish, dict)
    return publish
