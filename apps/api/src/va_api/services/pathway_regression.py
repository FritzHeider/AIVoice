"""Pathway regression suite settings (specs/12-pathways-api.md §9; Decision P07-T11e1 there).

The settings live in the draft's ``global_config.regression``, so saving, forking, duplicating, and exporting
a version carry them with the graph. Reads are lenient (:func:`va_domain.pathway_tests.load_regression`); a
write takes the pathway row lock, replaces the stored value, recomputes the draft's ``graph_hash``, and writes
a ``pathway.regression_update`` audit row, unless the stored value already equals the new one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from va_api.audit import audit
from va_api.auth.context import RequestContext
from va_db.models import PathwayVersion
from va_db.repositories import PathwayGraphJson, PathwayRepo
from va_domain.pathway_tests import (
    REGRESSION_KEY,
    RegressionSettings,
    load_regression,
    regression_summary,
    with_regression,
)
from va_engine.models.graph import graph_hash

__all__ = ["RegressionOutcome", "get_regression", "put_regression"]


@dataclass(frozen=True, slots=True)
class RegressionOutcome:
    """The draft after a write, the settings it holds, and whether anything changed."""

    draft: PathwayVersion
    settings: RegressionSettings
    changed: bool


async def get_regression(
    session: AsyncSession, ctx: RequestContext, pathway_id: uuid.UUID
) -> RegressionSettings:
    """The regression settings of the pathway's draft (defaults when none or an invalid value is stored)."""
    repo = PathwayRepo(session, ctx.org_id)
    draft = await repo.draft(await repo.get(pathway_id))
    return load_regression(draft.global_config)


async def put_regression(
    session: AsyncSession, ctx: RequestContext, pathway_id: uuid.UUID, settings: RegressionSettings
) -> RegressionOutcome:
    """Store ``settings`` in the draft's ``global_config`` under the pathway row lock."""
    repo = PathwayRepo(session, ctx.org_id)
    pathway = await repo.get(pathway_id, lock=True)
    draft = await repo.draft(pathway)
    stored = settings.model_dump(mode="json")
    if draft.global_config.get(REGRESSION_KEY) == stored:
        return RegressionOutcome(draft=draft, settings=settings, changed=False)
    before = {**regression_summary(load_regression(draft.global_config)), "graph_hash": draft.graph_hash}
    global_config = with_regression(draft.global_config, settings)
    nodes, edges = list(draft.nodes), list(draft.edges)
    graph = PathwayGraphJson(
        nodes=nodes,
        edges=edges,
        global_config=global_config,
        graph_hash=graph_hash({"nodes": nodes, "edges": edges, "global_config": global_config}),
    )
    await repo.update_draft(pathway, draft, values={}, graph=graph)
    after = {**regression_summary(settings), "graph_hash": draft.graph_hash}
    await audit(session, ctx, "pathway.regression_update", "pathway", pathway.id, before=before, after=after)
    return RegressionOutcome(draft=draft, settings=settings, changed=True)
