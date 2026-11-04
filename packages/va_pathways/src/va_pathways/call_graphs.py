"""The pathway graphs of a media-worker process (specs/11 §10; Decision P07-T12b under specs/11 §10).

:class:`CallGraphCache` serves the graph of the exact ``pathway_version`` a pathway call runs on. Published
and archived versions never change, so their parsed graphs stay in an in-process LRU of
:data:`DEFAULT_MAX_ENTRIES` entries keyed by ``(org_id, pathway_id, version)``; a draft version (a test call
of an unpublished pathway) is mutable and is read from the database on every call. Two concurrent misses of
one key may both load it (harmless: the graphs are equal). :meth:`CallGraphCache.loader` is the call's
:class:`~va_engine.navigation.PathwayLoader` for forwarding and Transfer Pathway nodes: numbered versions go
through the cache, ``production``/``staging`` pins are resolved in the database every time (a pin moves) and
the version they name is cached when frozen. Failures are
:class:`~va_engine.navigation.PathwayUnavailableError` (:func:`va_pathways.loader.load_pathway_graph`).
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from typing import Final

from va_db.session import Database
from va_engine.models.nodes import PathwayVersionRef
from va_engine.navigation import ActiveGraph, PathwayLoader
from va_pathways.loader import LoadedGraph, load_pathway_graph, pathway_uuid

__all__ = ["DEFAULT_MAX_ENTRIES", "CallGraphCache"]

DEFAULT_MAX_ENTRIES: Final = 128
"""Frozen pathway versions kept parsed per process."""

type _Key = tuple[uuid.UUID, uuid.UUID, int]


class CallGraphCache:
    """Parsed graphs of frozen pathway versions (see the module docstring)."""

    def __init__(self, database: Database, *, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        if max_entries < 1:
            msg = "max_entries must be at least 1"
            raise ValueError(msg)
        self._database = database
        self._max_entries = max_entries
        self._entries: OrderedDict[_Key, ActiveGraph] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    async def get(self, org_id: uuid.UUID, pathway_id: uuid.UUID, version: int) -> ActiveGraph:
        """Version ``version`` of the org's pathway (cached when frozen)."""
        key = (org_id, pathway_id, version)
        cached = self._entries.get(key)
        if cached is not None:
            self._entries.move_to_end(key)
            return cached
        loaded = await load_pathway_graph(self._database, org_id, pathway_id, version)
        return self._keep(org_id, loaded)

    async def resolve(self, org_id: uuid.UUID, pathway_id: uuid.UUID, ref: PathwayVersionRef) -> ActiveGraph:
        """A numbered version through :meth:`get`; a pin resolved now, its version cached when frozen."""
        if isinstance(ref, int):
            return await self.get(org_id, pathway_id, ref)
        return self._keep(org_id, await load_pathway_graph(self._database, org_id, pathway_id, ref))

    def loader(self, org_id: uuid.UUID) -> PathwayLoader:
        """The org's pathways for a running call (forwarding, Transfer Pathway)."""
        return _CachedLoader(self, org_id)

    def _keep(self, org_id: uuid.UUID, loaded: LoadedGraph) -> ActiveGraph:
        active = loaded.active
        if not loaded.frozen or active.pathway_id is None or active.version is None:
            return active
        key = (org_id, active.pathway_id, active.version)
        self._entries[key] = active
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
        return active


class _CachedLoader:
    """:class:`~va_engine.navigation.PathwayLoader` over a :class:`CallGraphCache` for one org."""

    def __init__(self, cache: CallGraphCache, org_id: uuid.UUID) -> None:
        self._cache = cache
        self._org_id = org_id

    async def load(self, pathway_id: str, version: PathwayVersionRef) -> ActiveGraph:
        return await self._cache.resolve(self._org_id, pathway_uuid(pathway_id), version)
