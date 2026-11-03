"""Tenant data access for Conversational Pathways (specs/12-pathways-api.md §1-§3; specs/03 §5).

:class:`PathwayRepo` works inside ``tenant_session(org_id)`` and still filters ``org_id`` on every query. A
pathway owns exactly one draft version (``pathways.draft_version``); creating a pathway inserts the row and
its draft v1 in the caller's transaction. Soft-deleted pathways and other orgs' rows read as
:class:`~va_db.repositories.base.NotFoundError`. :meth:`PathwayRepo.references` finds the live resources that
would break when the pathway goes away (specs/12 §3 "DELETE").

Versions (specs/12 §4; Decision P07-T09b): :meth:`PathwayRepo.freeze` turns a version into a saved
(``archived``) or ``published`` one, :meth:`PathwayRepo.fork_draft` inserts the next draft as a copy of a
version, and :meth:`PathwayRepo.set_pin` moves the ``production``/``staging`` pin. Callers hold the pathway
row lock (``get(..., lock=True)``) so that exactly one draft exists and ``draft_version == latest_version``.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from sqlalchemy import ColumnElement, func, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from va_common.ids import new_id
from va_common.time import Clock, SystemClock
from va_db.models import (
    Agent,
    AgentVersion,
    KnowledgeBase,
    Pathway,
    PathwayFolder,
    PathwayVersion,
    Persona,
    PersonaVersion,
    PhoneNumber,
    Tool,
    Widget,
)
from va_db.pagination import DEFAULT_PAGE_LIMIT, Page, decode_cursor, encode_cursor, validate_limit
from va_db.repositories.base import NotFoundError

__all__ = [
    "PATHWAY_RESOURCE",
    "PathwayGraphJson",
    "PathwayListRow",
    "PathwayRefIds",
    "PathwayReference",
    "PathwayRepo",
    "PinEnvironment",
    "ReferenceType",
    "escape_like",
]

PATHWAY_RESOURCE: Final = "Pathway"
"""The resource name of the pathway ``NOT_FOUND`` message ("Pathway not found.")."""

ReferenceType = Literal["phone_number", "agent", "persona", "widget"]
PinEnvironment = Literal["production", "staging"]

_LIVE_NUMBER_STATUSES: Final = ("pending", "active")
_LIKE_ESCAPE: Final = "\\"


@dataclass(frozen=True, slots=True)
class PathwayGraphJson:
    """The stored graph columns of a version."""

    nodes: list[Any]
    edges: list[Any]
    global_config: dict[str, Any]
    graph_hash: str

    @classmethod
    def of(cls, version: PathwayVersion) -> PathwayGraphJson:
        """A copy of the graph columns of ``version``."""
        return cls(
            nodes=list(version.nodes),
            edges=list(version.edges),
            global_config=dict(version.global_config),
            graph_hash=version.graph_hash,
        )


@dataclass(frozen=True, slots=True)
class PathwayListRow:
    """One list item: the pathway and the node count of its draft."""

    pathway: Pathway
    node_count: int


@dataclass(frozen=True, slots=True)
class PathwayReference:
    """A live resource pointing at a pathway (``name`` is the E.164 number for phone numbers)."""

    type: ReferenceType
    id: uuid.UUID
    name: str


@dataclass(frozen=True, slots=True)
class PathwayRefIds:
    """Ids of the org's live knowledge bases, tools, and pathways (validation reference checks)."""

    kb_ids: frozenset[uuid.UUID]
    tool_ids: frozenset[uuid.UUID]
    pathway_ids: frozenset[uuid.UUID]


def escape_like(term: str) -> str:
    """``term`` with the LIKE wildcards ``%``/``_`` and the escape character itself escaped."""
    return (
        term.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", f"{_LIKE_ESCAPE}%")
        .replace("_", f"{_LIKE_ESCAPE}_")
    )


class PathwayRepo:
    """Pathways and their versions of one org."""

    def __init__(self, session: AsyncSession, org_id: uuid.UUID, *, clock: Clock | None = None) -> None:
        self.session = session
        self.org_id = org_id
        self._clock: Clock = clock if clock is not None else SystemClock()

    async def create(
        self,
        *,
        name: str,
        description: str,
        folder_id: uuid.UUID | None,
        graph: PathwayGraphJson,
    ) -> tuple[Pathway, PathwayVersion]:
        """Insert a pathway with its draft version 1."""
        pathway = Pathway(
            id=new_id(),
            org_id=self.org_id,
            folder_id=folder_id,
            name=name,
            description=description,
            draft_version=1,
            latest_version=1,
        )
        self.session.add(pathway)
        await self.session.flush()
        draft = PathwayVersion(
            org_id=self.org_id,
            pathway_id=pathway.id,
            version_number=1,
            status="draft",
            nodes=graph.nodes,
            edges=graph.edges,
            global_config=graph.global_config,
            graph_hash=graph.graph_hash,
        )
        self.session.add(draft)
        await self.session.flush()
        await self.session.refresh(pathway)
        await self.session.refresh(draft)
        return pathway, draft

    async def get(self, pathway_id: uuid.UUID, *, lock: bool = False) -> Pathway:
        """The org's live pathway (``SELECT … FOR UPDATE`` with ``lock``); else ``NOT_FOUND``."""
        statement = select(Pathway).where(
            Pathway.org_id == self.org_id, Pathway.id == pathway_id, Pathway.deleted_at.is_(None)
        )
        if lock:
            statement = statement.with_for_update()
        pathway = await self.session.scalar(statement)
        if pathway is None:
            raise NotFoundError(PATHWAY_RESOURCE)
        return pathway

    async def version(self, pathway: Pathway, number: int) -> PathwayVersion | None:
        """Version ``number`` of ``pathway``, or ``None``."""
        statement = select(PathwayVersion).where(
            PathwayVersion.org_id == self.org_id,
            PathwayVersion.pathway_id == pathway.id,
            PathwayVersion.version_number == number,
        )
        return await self.session.scalar(statement)

    async def versions(self, pathway: Pathway) -> list[PathwayVersion]:
        """Every version of ``pathway``, newest first."""
        statement = (
            select(PathwayVersion)
            .where(PathwayVersion.org_id == self.org_id, PathwayVersion.pathway_id == pathway.id)
            .order_by(PathwayVersion.version_number.desc())
        )
        return list((await self.session.scalars(statement)).all())

    async def freeze(
        self,
        version: PathwayVersion,
        *,
        status: Literal["archived", "published"],
        name: str | None = None,
        published_by: uuid.UUID | None = None,
    ) -> None:
        """Set ``status`` (and ``name`` when given); the first publish records ``published_at``/``by``."""
        version.status = status
        if name is not None:
            version.name = name
        if status == "published" and version.published_at is None:
            version.published_at = self._clock.now()
            version.published_by = published_by
        await self.session.flush()

    async def fork_draft(self, pathway: Pathway, source: PathwayVersion) -> PathwayVersion:
        """Insert draft ``latest_version + 1`` copied from ``source`` and point the pathway at it.

        Freeze the previous draft first: the pathway keeps exactly one ``draft`` row.
        """
        number = pathway.latest_version + 1
        graph = PathwayGraphJson.of(source)
        draft = PathwayVersion(
            org_id=self.org_id,
            pathway_id=pathway.id,
            version_number=number,
            status="draft",
            nodes=graph.nodes,
            edges=graph.edges,
            global_config=graph.global_config,
            graph_hash=graph.graph_hash,
        )
        self.session.add(draft)
        pathway.draft_version = number
        pathway.latest_version = number
        await self.session.flush()
        await self.session.refresh(pathway)
        await self.session.refresh(draft)
        return draft

    async def set_pin(self, pathway: Pathway, environment: PinEnvironment, number: int) -> None:
        """Pin version ``number`` to ``environment``."""
        if environment == "production":
            pathway.production_version = number
        else:
            pathway.staging_version = number
        await self.session.flush()
        await self.session.refresh(pathway)

    async def draft(self, pathway: Pathway) -> PathwayVersion:
        """The draft version of ``pathway`` (every pathway has one; a missing row is a data error)."""
        draft = await self.version(pathway, pathway.draft_version)
        if draft is None:
            msg = f"pathway {pathway.id} has no draft version {pathway.draft_version}"
            raise LookupError(msg)
        return draft

    async def page(
        self,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
        cursor: str | None = None,
        folder_id: uuid.UUID | None = None,
        search: str | None = None,
    ) -> Page[PathwayListRow]:
        """Live pathways newest first (``created_at DESC, id DESC``) with their draft's node count."""
        validate_limit(limit)
        node_count = func.coalesce(func.jsonb_array_length(PathwayVersion.nodes), 0)
        statement = (
            select(Pathway, node_count)
            .outerjoin(
                PathwayVersion,
                (PathwayVersion.pathway_id == Pathway.id)
                & (PathwayVersion.version_number == Pathway.draft_version)
                & (PathwayVersion.org_id == self.org_id),
            )
            .where(*self._list_filters(folder_id, search))
        )
        if cursor is not None:
            key = decode_cursor(cursor)
            statement = statement.where(
                tuple_(Pathway.created_at, Pathway.id) < tuple_(key.created_at, key.id)
            )
        statement = statement.order_by(Pathway.created_at.desc(), Pathway.id.desc()).limit(limit + 1)
        rows = [
            PathwayListRow(pathway, int(count)) for pathway, count in await self.session.execute(statement)
        ]
        items, has_more = tuple(rows[:limit]), len(rows) > limit
        next_cursor = None
        if has_more:
            last = items[-1].pathway
            next_cursor = encode_cursor(last.created_at, last.id)
        return Page(items=items, next_cursor=next_cursor, has_more=has_more)

    def _list_filters(self, folder_id: uuid.UUID | None, search: str | None) -> list[ColumnElement[bool]]:
        filters: list[ColumnElement[bool]] = [Pathway.org_id == self.org_id, Pathway.deleted_at.is_(None)]
        if folder_id is not None:
            filters.append(Pathway.folder_id == folder_id)
        if search:
            filters.append(Pathway.name.ilike(f"%{escape_like(search)}%", escape=_LIKE_ESCAPE))
        return filters

    async def folder_exists(self, folder_id: uuid.UUID) -> bool:
        """Whether the org has the folder ``folder_id``."""
        statement = select(PathwayFolder.id).where(
            PathwayFolder.org_id == self.org_id, PathwayFolder.id == folder_id
        )
        return await self.session.scalar(statement) is not None

    async def update_draft(
        self,
        pathway: Pathway,
        draft: PathwayVersion,
        *,
        values: Mapping[str, Any],
        graph: PathwayGraphJson | None,
    ) -> None:
        """Apply pathway column ``values`` and the draft ``graph``; ``updated_at`` always moves."""
        for key, value in values.items():
            setattr(pathway, key, value)
        if graph is not None:
            draft.nodes = graph.nodes
            draft.edges = graph.edges
            draft.global_config = graph.global_config
            draft.graph_hash = graph.graph_hash
        await self.session.flush()
        # The trigger sets updated_at on any UPDATE, also when only the draft's graph changed.
        await self.session.execute(
            update(Pathway)
            .where(Pathway.org_id == self.org_id, Pathway.id == pathway.id)
            .values(updated_at=func.now())
        )
        await self.session.refresh(pathway)
        await self.session.refresh(draft)

    async def soft_delete(self, pathway: Pathway) -> None:
        """Set ``deleted_at`` on ``pathway``."""
        pathway.deleted_at = self._clock.now()
        await self.session.flush()

    async def references(self, pathway_id: uuid.UUID) -> list[PathwayReference]:
        """Live phone numbers, pinned agents, persona production versions, and widgets using the pathway."""
        return [
            *await self._number_refs(pathway_id),
            *await self._agent_refs(pathway_id),
            *await self._persona_refs(pathway_id),
            *await self._widget_refs(pathway_id),
        ]

    async def _number_refs(self, pathway_id: uuid.UUID) -> list[PathwayReference]:
        statement = (
            select(PhoneNumber.id, PhoneNumber.e164)
            .where(
                PhoneNumber.org_id == self.org_id,
                PhoneNumber.deleted_at.is_(None),
                PhoneNumber.status.in_(_LIVE_NUMBER_STATUSES),
                PhoneNumber.inbound_config["pathway_id"].astext == str(pathway_id),
            )
            .order_by(PhoneNumber.e164, PhoneNumber.id)
        )
        return [
            PathwayReference("phone_number", row_id, e164)
            for row_id, e164 in await self.session.execute(statement)
        ]

    async def _agent_refs(self, pathway_id: uuid.UUID) -> list[PathwayReference]:
        statement = (
            select(Agent, AgentVersion.semver)
            .join(AgentVersion, AgentVersion.agent_id == Agent.id)
            .where(
                Agent.org_id == self.org_id,
                Agent.deleted_at.is_(None),
                AgentVersion.org_id == self.org_id,
                AgentVersion.pathway_id == pathway_id,
            )
            .order_by(Agent.name, Agent.id)
        )
        pinned: dict[uuid.UUID, PathwayReference] = {}
        for agent, semver in await self.session.execute(statement):
            if semver in _pinned_semvers(agent.env_pins):
                pinned.setdefault(agent.id, PathwayReference("agent", agent.id, agent.name))
        return list(pinned.values())

    async def _persona_refs(self, pathway_id: uuid.UUID) -> list[PathwayReference]:
        statement = (
            select(Persona.id, Persona.name)
            .join(
                PersonaVersion,
                (PersonaVersion.persona_id == Persona.id)
                & (PersonaVersion.version_number == Persona.production_version),
            )
            .where(
                Persona.org_id == self.org_id,
                Persona.deleted_at.is_(None),
                PersonaVersion.org_id == self.org_id,
                PersonaVersion.config["pathway_id"].astext == str(pathway_id),
            )
            .order_by(Persona.name, Persona.id)
        )
        return [
            PathwayReference("persona", row_id, name)
            for row_id, name in await self.session.execute(statement)
        ]

    async def _widget_refs(self, pathway_id: uuid.UUID) -> list[PathwayReference]:
        statement = (
            select(Widget.id, Widget.name)
            .where(Widget.org_id == self.org_id, Widget.deleted_at.is_(None), Widget.pathway_id == pathway_id)
            .order_by(Widget.name, Widget.id)
        )
        return [
            PathwayReference("widget", row_id, name) for row_id, name in await self.session.execute(statement)
        ]

    async def ref_ids(self) -> PathwayRefIds:
        """The ids of the org's live knowledge bases, tools, and pathways."""
        return PathwayRefIds(
            kb_ids=await self._live_ids(KnowledgeBase.id, KnowledgeBase.org_id, KnowledgeBase.deleted_at),
            tool_ids=await self._live_ids(Tool.id, Tool.org_id, Tool.deleted_at),
            pathway_ids=await self._live_ids(Pathway.id, Pathway.org_id, Pathway.deleted_at),
        )

    async def _live_ids(self, *columns: Any) -> frozenset[uuid.UUID]:
        row_id, org_id, deleted_at = columns
        statement = select(row_id).where(org_id == self.org_id, deleted_at.is_(None))
        return frozenset((await self.session.scalars(statement)).all())


def _pinned_semvers(env_pins: Mapping[str, Any] | Sequence[Any] | None) -> frozenset[str]:
    """The semver strings an agent's ``env_pins`` pin (non-string values are ignored)."""
    if not isinstance(env_pins, Mapping):
        return frozenset()
    return frozenset(value for value in env_pins.values() if isinstance(value, str))
