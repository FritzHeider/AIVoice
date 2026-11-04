"""Loads another pathway of the org for a running conversation (specs/11 §4 forwarding / Transfer Pathway).

:class:`DbPathwayLoader` implements :class:`va_engine.navigation.PathwayLoader` over the org's pathways in a
tenant session: ``production``/``staging`` resolve the pathway's pin, a number selects that version. A
pathway that is absent, deleted, of another org, or not a UUID, an unset pin, a missing version, and a stored
graph that no longer parses are all :class:`~va_engine.navigation.PathwayUnavailableError` (the engine
speaks its error phrase or stays; it never raises). Used by the pathway test chat (specs/12 §7 Decision
P07-T10), the unit-test runner (specs/12 §8 Decision P07-T11a) and, through :func:`load_pathway_graph`, the
call graph cache of pathway calls (:mod:`va_pathways.call_graphs`, specs/11 §10 Decision P07-T12b).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from pydantic import ValidationError

from va_db.models import PathwayVersion
from va_db.repositories import PathwayRepo
from va_db.repositories.base import NotFoundError
from va_db.session import Database
from va_engine.models import PathwayGraph, load_graph
from va_engine.models.nodes import PathwayVersionRef
from va_engine.navigation import ActiveGraph, PathwayUnavailableError

__all__ = ["DbPathwayLoader", "LoadedGraph", "load_pathway_graph", "pathway_uuid"]


@dataclass(frozen=True, slots=True)
class LoadedGraph:
    """A loaded version's graph; ``frozen``: the version is no longer a draft (its graph never changes)."""

    active: ActiveGraph
    frozen: bool


def _graph_of(version: PathwayVersion) -> PathwayGraph:
    return load_graph(list(version.nodes), list(version.edges), dict(version.global_config))


def pathway_uuid(pathway_id: str) -> uuid.UUID:
    """``pathway_id`` as a UUID; :class:`PathwayUnavailableError` ``not_found`` when it is not one."""
    try:
        return uuid.UUID(pathway_id)
    except ValueError as exc:
        raise PathwayUnavailableError("not_found") from exc


async def load_pathway_graph(
    database: Database, org_id: uuid.UUID, pathway_id: uuid.UUID, version: PathwayVersionRef
) -> LoadedGraph:
    """Version ``version`` (a number, or the ``production``/``staging`` pin) of the org's pathway, parsed in
    a worker thread; :class:`PathwayUnavailableError` as described in the module docstring."""
    async with database.tenant_session(org_id) as session:
        repo = PathwayRepo(session, org_id)
        try:
            pathway = await repo.get(pathway_id)
        except NotFoundError as exc:
            raise PathwayUnavailableError("not_found") from exc
        if version == "production":
            number = pathway.production_version
        elif version == "staging":
            number = pathway.staging_version
        else:
            number = version
        stored = None if number is None else await repo.version(pathway, number)
        if stored is None or number is None:
            raise PathwayUnavailableError("version_not_found")
        try:
            graph = await asyncio.to_thread(_graph_of, stored)
        except ValidationError as exc:
            raise PathwayUnavailableError("invalid_graph") from exc
        frozen = stored.status != "draft"
    return LoadedGraph(ActiveGraph(pathway_id, number, graph), frozen)


class DbPathwayLoader:
    """The pathways of ``org_id`` as engine graphs."""

    def __init__(self, database: Database, org_id: uuid.UUID) -> None:
        self._database = database
        self._org_id = org_id

    async def load(self, pathway_id: str, version: PathwayVersionRef) -> ActiveGraph:
        loaded = await load_pathway_graph(self._database, self._org_id, pathway_uuid(pathway_id), version)
        return loaded.active
