"""Tenant data access for custom tools (specs/13-tools-and-dynamic-data.md §2; specs/03 §5 ``tools``).

Rules: specs/13 §2 Decision P08-T01. :class:`ToolRepo` works inside ``tenant_session(org_id)`` and still
filters ``org_id`` on every query. Soft-deleted and other orgs' tools read as ``TOOL_NOT_FOUND``. The specs/03
``UNIQUE (org_id, name)`` is not partial, so a name still held by a soft-deleted tool is freed by deleting
that row for good in the same transaction (nothing references ``tools.id`` by foreign key).
:meth:`ToolRepo.references` lists the published pathway versions, published persona versions, and versions of
live agents whose tool lists hold the tool, in either id form (``TL-<uuid>`` or the bare uuid).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from sqlalchemy import ColumnElement, cast, delete, or_, select, tuple_
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from va_common.errors import AppError
from va_common.ids import PublicPrefix, new_id, to_public
from va_common.time import Clock, SystemClock
from va_db.models import Agent, AgentVersion, Pathway, PathwayVersion, Persona, PersonaVersion, Tool
from va_db.pagination import DEFAULT_PAGE_LIMIT, Page, decode_cursor, encode_cursor, validate_limit
from va_db.repositories.base import conflict_savepoint
from va_db.repositories.pathways import escape_like

__all__ = [
    "MAX_TOOL_REFERENCES",
    "TOOL_NOT_FOUND_MESSAGE",
    "ToolReference",
    "ToolReferenceType",
    "ToolRepo",
    "tool_name_conflict",
]

TOOL_NOT_FOUND_MESSAGE: Final = "Tool not found."
MAX_TOOL_REFERENCES: Final = 20
_LIKE_ESCAPE: Final = "\\"

ToolReferenceType = Literal["pathway", "persona", "agent"]


@dataclass(frozen=True, slots=True)
class ToolReference:
    """A published resource version whose tool list holds the tool (``version``: number or semver)."""

    type: ToolReferenceType
    id: uuid.UUID
    version: str


def tool_name_conflict(name: str) -> str:
    """The ``CONFLICT`` message for a name a live tool of the org already has."""
    return f"A tool named '{name}' already exists."


def _not_found() -> AppError:
    return AppError("TOOL_NOT_FOUND", TOOL_NOT_FOUND_MESSAGE)


def _id_forms(tool_id: uuid.UUID) -> tuple[str, str]:
    return to_public(PublicPrefix.TOOL, tool_id), str(tool_id)


def _jsonb(value: Any) -> ColumnElement[Any]:
    return cast(value, JSONB)


class ToolRepo:
    """The custom tools of one org."""

    def __init__(self, session: AsyncSession, org_id: uuid.UUID, *, clock: Clock | None = None) -> None:
        self.session, self.org_id = session, org_id
        self._clock: Clock = clock or SystemClock()

    async def get(self, tool_id: uuid.UUID, *, lock: bool = False) -> Tool:
        """The org's live tool (``SELECT … FOR UPDATE`` with ``lock``); else ``TOOL_NOT_FOUND``."""
        statement = select(Tool).where(
            Tool.org_id == self.org_id, Tool.id == tool_id, Tool.deleted_at.is_(None)
        )
        if lock:
            statement = statement.with_for_update()
        tool = await self.session.scalar(statement)
        if tool is None:
            raise _not_found()
        return tool

    async def by_ids(self, tool_ids: Sequence[uuid.UUID]) -> list[Tool]:
        """The org's live tools among ``tool_ids`` in that order (absent, deleted and foreign ids skipped)."""
        if not tool_ids:
            return []
        statement = select(Tool).where(
            Tool.org_id == self.org_id, Tool.id.in_(list(tool_ids)), Tool.deleted_at.is_(None)
        )
        rows = {row.id: row for row in (await self.session.scalars(statement)).all()}
        return [rows[tool_id] for tool_id in dict.fromkeys(tool_ids) if tool_id in rows]

    async def page(
        self, *, limit: int = DEFAULT_PAGE_LIMIT, cursor: str | None = None, search: str | None = None
    ) -> Page[Tool]:
        """Live tools newest first (``created_at DESC, id DESC``); ``search`` matches name or description."""
        validate_limit(limit)
        statement = select(Tool).where(*self._list_filters(search))
        if cursor is not None:
            key = decode_cursor(cursor)
            statement = statement.where(tuple_(Tool.created_at, Tool.id) < tuple_(key.created_at, key.id))
        statement = statement.order_by(Tool.created_at.desc(), Tool.id.desc()).limit(limit + 1)
        rows = list((await self.session.scalars(statement)).all())
        items, has_more = tuple(rows[:limit]), len(rows) > limit
        next_cursor = encode_cursor(items[-1].created_at, items[-1].id) if has_more else None
        return Page(items=items, next_cursor=next_cursor, has_more=has_more)

    def _list_filters(self, search: str | None) -> list[ColumnElement[bool]]:
        filters: list[ColumnElement[bool]] = [Tool.org_id == self.org_id, Tool.deleted_at.is_(None)]
        if search:
            pattern = f"%{escape_like(search)}%"
            filters.append(
                or_(
                    Tool.name.ilike(pattern, escape=_LIKE_ESCAPE),
                    Tool.description.ilike(pattern, escape=_LIKE_ESCAPE),
                )
            )
        return filters

    async def create(self, values: Mapping[str, Any], *, tool_id: uuid.UUID | None = None) -> Tool:
        """Insert a tool with the column ``values``; ``CONFLICT`` (field ``name``) for a live namesake."""
        name = str(values["name"])
        await self._free_deleted_name(name)
        tool = Tool(id=tool_id if tool_id is not None else new_id(), org_id=self.org_id, **values)
        async with conflict_savepoint(self.session, tool_name_conflict(name), field="name"):
            self.session.add(tool)
        await self.session.refresh(tool)
        return tool

    async def update(self, tool: Tool, values: Mapping[str, Any]) -> Tool:
        """Set the column ``values`` on ``tool``; a rename onto a live namesake is ``CONFLICT``."""
        name = values.get("name")
        if isinstance(name, str) and name != tool.name:
            await self._free_deleted_name(name)
        tool_id = tool.id
        async with conflict_savepoint(self.session, tool_name_conflict(str(name)), field="name"):
            for key, value in values.items():
                setattr(tool, key, value)
        return await self._reload(tool_id)

    async def _reload(self, tool_id: uuid.UUID) -> Tool:
        tool = await self.session.scalar(
            select(Tool)
            .where(Tool.org_id == self.org_id, Tool.id == tool_id)
            .execution_options(populate_existing=True)
        )
        if tool is None:
            raise _not_found()
        return tool

    async def _free_deleted_name(self, name: str) -> None:
        await self.session.execute(
            delete(Tool).where(Tool.org_id == self.org_id, Tool.name == name, Tool.deleted_at.is_not(None))
        )

    async def soft_delete(self, tool: Tool) -> None:
        """Set ``deleted_at`` on ``tool``."""
        tool.deleted_at = self._clock.now()
        await self.session.flush()

    async def references(self, tool_id: uuid.UUID) -> list[ToolReference]:
        """Up to 20 published pathway/persona versions and live agents' versions listing the tool."""
        found = [
            *await self._pathway_refs(tool_id),
            *await self._persona_refs(tool_id),
            *await self._agent_refs(tool_id),
        ]
        return found[:MAX_TOOL_REFERENCES]

    async def _pathway_refs(self, tool_id: uuid.UUID) -> list[ToolReference]:
        holds = [
            condition
            for ref in _id_forms(tool_id)
            for condition in (
                PathwayVersion.nodes.op("@>")(_jsonb([{"data": {"tools": [ref]}}])),
                PathwayVersion.global_config.op("@>")(_jsonb({"globalTools": [ref]})),
            )
        ]
        statement = (
            select(Pathway.id, PathwayVersion.version_number)
            .join(PathwayVersion, PathwayVersion.pathway_id == Pathway.id)
            .where(
                Pathway.org_id == self.org_id,
                Pathway.deleted_at.is_(None),
                PathwayVersion.org_id == self.org_id,
                PathwayVersion.status == "published",
                or_(*holds),
            )
            .order_by(Pathway.id, PathwayVersion.version_number)
            .limit(MAX_TOOL_REFERENCES)
        )
        return [
            ToolReference("pathway", row_id, str(number))
            for row_id, number in await self.session.execute(statement)
        ]

    async def _persona_refs(self, tool_id: uuid.UUID) -> list[ToolReference]:
        holds = [PersonaVersion.config.op("@>")(_jsonb({"tools": [ref]})) for ref in _id_forms(tool_id)]
        statement = (
            select(Persona.id, PersonaVersion.version_number)
            .join(PersonaVersion, PersonaVersion.persona_id == Persona.id)
            .where(
                Persona.org_id == self.org_id,
                Persona.deleted_at.is_(None),
                PersonaVersion.org_id == self.org_id,
                PersonaVersion.status == "published",
                or_(*holds),
            )
            .order_by(Persona.id, PersonaVersion.version_number)
            .limit(MAX_TOOL_REFERENCES)
        )
        return [
            ToolReference("persona", row_id, str(number))
            for row_id, number in await self.session.execute(statement)
        ]

    async def _agent_refs(self, tool_id: uuid.UUID) -> list[ToolReference]:
        holds = [AgentVersion.call_settings.op("@>")(_jsonb({"tools": [ref]})) for ref in _id_forms(tool_id)]
        statement = (
            select(Agent.id, AgentVersion.semver)
            .join(AgentVersion, AgentVersion.agent_id == Agent.id)
            .where(
                Agent.org_id == self.org_id,
                Agent.deleted_at.is_(None),
                AgentVersion.org_id == self.org_id,
                or_(*holds),
            )
            .order_by(Agent.id, AgentVersion.created_at)
            .limit(MAX_TOOL_REFERENCES)
        )
        return [
            ToolReference("agent", row_id, semver) for row_id, semver in await self.session.execute(statement)
        ]
