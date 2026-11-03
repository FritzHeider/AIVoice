"""Pathway export, import, and duplicate (specs/12-pathways-api.md §4 duplicate, §5).

Rules: specs/12 §6 Decision P07-T09c items 2-4. Every function runs in the caller's ``tenant_session``.
Imported and duplicated graphs go through the same save-mode check as a created pathway
(:func:`va_api.services.pathways.create_checked`): node type aliases and ``transferNumber`` are normalised by
:func:`va_engine.models.load_graph` (specs/11 §3.3), and missing tools/knowledge bases are kept and reported.
Export bodies hold the stored (``dump_graph``) graph, so an export imports again with the same ``graph_hash``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from va_api.auth.context import RequestContext
from va_api.services.pathways import (
    GraphInput,
    PathwayCreation,
    VersionSelector,
    create_checked,
    load_version,
)
from va_engine.validation import KB_MISSING, TOOL_MISSING, IssueItem, ValidationReport

__all__ = [
    "BLAND_FORMAT",
    "COPY_PREFIX",
    "DEFAULT_IMPORT_NAME",
    "EXPORT_FORMAT",
    "PathwayExport",
    "PathwayImport",
    "copy_name",
    "duplicate_pathway",
    "export_pathway",
    "import_pathway",
    "missing_reference_warnings",
]

EXPORT_FORMAT: Final = "voiceagents.pathway.v1"
BLAND_FORMAT: Final = "bland"
DEFAULT_IMPORT_NAME: Final = "Imported pathway"
COPY_PREFIX: Final = "Copy of "
_MAX_NAME_LENGTH: Final = 200
_MISSING_REFERENCE_CODES: Final = frozenset({TOOL_MISSING, KB_MISSING})


@dataclass(frozen=True, slots=True)
class PathwayExport:
    """The ``voiceagents.pathway.v1`` document of one version."""

    name: str
    description: str
    nodes: list[Any]
    edges: list[Any]
    global_config: dict[str, Any]
    exported_at: datetime


@dataclass(frozen=True, slots=True)
class PathwayImport:
    """An import body; ``format`` is ``None`` for a Bland export."""

    graph: GraphInput
    name: str | None = None
    description: str | None = None
    folder_id: uuid.UUID | None = None
    format: Literal["voiceagents.pathway.v1"] | None = None


def copy_name(source_name: str) -> str:
    """The default duplicate name: ``Copy of <source>`` cut to 200 characters."""
    return f"{COPY_PREFIX}{source_name}"[:_MAX_NAME_LENGTH]


def missing_reference_warnings(report: ValidationReport) -> list[IssueItem]:
    """The validation warnings about tools and knowledge bases the org does not have."""
    return [item for item in report["warnings"] if item.get("code") in _MISSING_REFERENCE_CODES]


async def export_pathway(
    session: AsyncSession,
    ctx: RequestContext,
    pathway_id: uuid.UUID,
    selector: VersionSelector,
    *,
    now: datetime,
) -> PathwayExport:
    """The export document of the selected version (``PATHWAY_VERSION_NOT_FOUND`` as for ``GET``)."""
    pathway, version = await load_version(session, ctx, pathway_id, selector)
    return PathwayExport(
        name=pathway.name,
        description=pathway.description,
        nodes=list(version.nodes),
        edges=list(version.edges),
        global_config=dict(version.global_config),
        exported_at=now,
    )


async def import_pathway(
    session: AsyncSession, ctx: RequestContext, body: PathwayImport, *, allow_http: bool
) -> PathwayCreation:
    """Create a pathway (draft v1) from an export of ours or of Bland (``pathway.import``)."""
    return await create_checked(
        session,
        ctx,
        name=body.name or DEFAULT_IMPORT_NAME,
        description=body.description or "",
        folder_id=body.folder_id,
        graph_input=body.graph,
        allow_http=allow_http,
        action="pathway.import",
        audit_extra={"format": body.format or BLAND_FORMAT},
    )


async def duplicate_pathway(  # noqa: PLR0913 - keyword-only body fields and URL policy
    session: AsyncSession,
    ctx: RequestContext,
    pathway_id: uuid.UUID,
    *,
    name: str | None,
    selector: VersionSelector,
    allow_http: bool,
) -> PathwayCreation:
    """A new pathway whose draft v1 copies the selected version (``pathway.duplicate``)."""
    source, version = await load_version(session, ctx, pathway_id, selector)
    return await create_checked(
        session,
        ctx,
        name=name or copy_name(source.name),
        description=source.description,
        folder_id=source.folder_id,
        graph_input=GraphInput.of(version),
        allow_http=allow_http,
        action="pathway.duplicate",
        audit_extra={"source_pathway_id": str(source.id), "source_version": version.version_number},
    )
