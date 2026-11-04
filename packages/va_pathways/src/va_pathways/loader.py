"""Loads another pathway of the org for a running conversation (specs/11 §4 forwarding / Transfer Pathway).

:class:`DbPathwayLoader` implements :class:`va_engine.navigation.PathwayLoader` over the org's pathways in a
tenant session: ``production``/``staging`` resolve the pathway's pin, a number selects that version. A
pathway that is absent, deleted, of another org, or not a UUID, an unset pin, a missing version, and a stored
graph that no longer parses are all :class:`~va_engine.navigation.PathwayUnavailableError` (the engine
speaks its error phrase or stays; it never raises). Used by the pathway test chat (specs/12 §7 Decision
P07-T10) and the unit-test runner (specs/12 §8 Decision P07-T11a).
"""

from __future__ import annotations

import asyncio
import uuid

from pydantic import ValidationError

from va_db.models import PathwayVersion
from va_db.repositories import PathwayRepo
from va_db.repositories.base import NotFoundError
from va_db.session import Database
from va_engine.models import PathwayGraph, load_graph
from va_engine.models.nodes import PathwayVersionRef
from va_engine.navigation import ActiveGraph, PathwayUnavailableError

__all__ = ["DbPathwayLoader"]


def _graph_of(version: PathwayVersion) -> PathwayGraph:
    return load_graph(list(version.nodes), list(version.edges), dict(version.global_config))


class DbPathwayLoader:
    """The pathways of ``org_id`` as engine graphs."""

    def __init__(self, database: Database, org_id: uuid.UUID) -> None:
        self._database = database
        self._org_id = org_id

    async def load(self, pathway_id: str, version: PathwayVersionRef) -> ActiveGraph:
        try:
            parsed = uuid.UUID(pathway_id)
        except ValueError as exc:
            raise PathwayUnavailableError("not_found") from exc
        async with self._database.tenant_session(self._org_id) as session:
            repo = PathwayRepo(session, self._org_id)
            try:
                pathway = await repo.get(parsed)
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
        return ActiveGraph(parsed, number, graph)
