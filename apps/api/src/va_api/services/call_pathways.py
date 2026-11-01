"""Pathway and version loading for Send Call (specs/05-calls-api.md §3.1, §4.1 step 5; Decision P05-T03c).

Every query runs in the caller's tenant session and filters by ``org_id``; another org's pathway reads as
absent (RLS and the explicit filter agree).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from va_common.errors import AppError
from va_db.models import Pathway, PathwayVersion
from va_domain.call_resolution import PathwayRef, ResolutionWarning, pathway_call_defaults

__all__ = [
    "AGENT_PATHWAY_FIELDS",
    "PERSONA_CONFIG_FIELDS",
    "REQUEST_PATHWAY_FIELDS",
    "LoadedPathway",
    "PathwayFields",
    "PathwaySelector",
    "draft_requires_test_call",
    "load_pathway",
    "start_node_of",
]

type PathwaySelector = int | Literal["production", "staging", "draft"] | None


@dataclass(frozen=True, slots=True)
class PathwayFields:
    """Error `field` names for one source of a pathway reference (request, persona config, agent version)."""

    pathway_id: str
    version: str
    start_node_id: str


REQUEST_PATHWAY_FIELDS: Final = PathwayFields("pathway_id", "pathway_version", "start_node_id")
PERSONA_CONFIG_FIELDS: Final = PathwayFields(
    "persona.config.pathway_id", "persona.config.pathway_version", "persona.config.start_node_id"
)
AGENT_PATHWAY_FIELDS: Final = PathwayFields("agent_version", "agent_version", "start_node_id")


@dataclass(frozen=True, slots=True)
class LoadedPathway:
    ref: PathwayRef
    warnings: tuple[ResolutionWarning, ...] = ()


def draft_requires_test_call(field: str) -> AppError:
    return AppError("INVALID_INPUT", f'{field} "draft" requires test_call.', field=field)


def start_node_of(nodes: Sequence[Any]) -> str | None:
    """The id of the node whose `data.isStart` is true (specs/11 §2), `None` when there is none."""
    for node in nodes:
        if (
            isinstance(node, Mapping)
            and isinstance(node.get("data"), Mapping)
            and node["data"].get("isStart")
        ):
            node_id = node.get("id")
            return node_id if isinstance(node_id, str) else None
    return None


def _node_ids(nodes: Sequence[Any]) -> set[str]:
    return {node["id"] for node in nodes if isinstance(node, Mapping) and isinstance(node.get("id"), str)}


async def _version_row(session: AsyncSession, pathway: Pathway, number: int) -> PathwayVersion | None:
    return await session.scalar(
        select(PathwayVersion).where(
            PathwayVersion.org_id == pathway.org_id,
            PathwayVersion.pathway_id == pathway.id,
            PathwayVersion.version_number == number,
        )
    )


def _not_found(number: int, fields: PathwayFields) -> AppError:
    return AppError("PATHWAY_VERSION_NOT_FOUND", f"Pathway version {number} not found.", field=fields.version)


async def _required_row(
    session: AsyncSession, pathway: Pathway, number: int, fields: PathwayFields
) -> PathwayVersion:
    row = await _version_row(session, pathway, number)
    if row is None:
        raise _not_found(number, fields)
    return row


def _pinned(
    pathway: Pathway, selector: PathwaySelector, *, test_call: bool, fields: PathwayFields
) -> tuple[int, ResolutionWarning | None]:
    """The version number of a non-integer selector: `draft`, `staging` (else production), production."""
    if selector == "draft":
        if not test_call:
            raise draft_requires_test_call(fields.version)
        return pathway.draft_version, None
    if selector == "staging" and pathway.staging_version is not None:
        return pathway.staging_version, None
    if pathway.production_version is not None:
        return pathway.production_version, None
    message = "The pathway has no production version; its draft is used."
    return pathway.draft_version, ResolutionWarning("PATHWAY_UNPUBLISHED", message, fields.version)


async def _numbered(
    session: AsyncSession, pathway: Pathway, number: int, *, exact: bool, fields: PathwayFields
) -> tuple[PathwayVersion, ResolutionWarning | None]:
    """An integer version; when absent, production with `PATHWAY_VERSION_FALLBACK` unless `exact`."""
    row = await _version_row(session, pathway, number)
    if row is not None:
        return row, None
    if exact or pathway.production_version is None:
        raise _not_found(number, fields)
    message = f"Pathway version {number} does not exist; the production version is used."
    warning = ResolutionWarning("PATHWAY_VERSION_FALLBACK", message, fields.version)
    return await _required_row(session, pathway, pathway.production_version, fields), warning


async def _selected(  # noqa: PLR0913 - keyword-only selection inputs
    session: AsyncSession,
    pathway: Pathway,
    selector: PathwaySelector,
    *,
    test_call: bool,
    exact: bool,
    fields: PathwayFields,
) -> tuple[PathwayVersion, ResolutionWarning | None]:
    """The version row a selector designates, with the warning the choice produces."""
    if isinstance(selector, int) and selector > 0:
        return await _numbered(session, pathway, selector, exact=exact, fields=fields)
    number, warning = _pinned(pathway, selector, test_call=test_call, fields=fields)
    return await _required_row(session, pathway, number, fields), warning


def _start_node(version: PathwayVersion, start_node_id: str | None, fields: PathwayFields) -> str | None:
    if start_node_id is None:
        return start_node_of(version.nodes)
    if start_node_id not in _node_ids(version.nodes):
        message = f"start_node_id does not exist in pathway version {version.version_number}."
        raise AppError("INVALID_INPUT", message, field=fields.start_node_id)
    return start_node_id


async def load_pathway(  # noqa: PLR0913 - keyword-only selection inputs
    session: AsyncSession,
    org_id: uuid.UUID,
    pathway_id: uuid.UUID,
    selector: PathwaySelector,
    *,
    fields: PathwayFields,
    test_call: bool = False,
    start_node_id: str | None = None,
    exact: bool = False,
) -> LoadedPathway:
    """Resolve a pathway reference to one version (specs/05 §3.1 `pathway_version` rules).

    `exact` (agent versions) turns a missing integer version into `PATHWAY_VERSION_NOT_FOUND` instead of the
    production fallback. `start_node_id` must name a node of the chosen version.
    """
    pathway = await session.scalar(
        select(Pathway).where(
            Pathway.id == pathway_id, Pathway.org_id == org_id, Pathway.deleted_at.is_(None)
        )
    )
    if pathway is None:
        raise AppError("NOT_FOUND", "Pathway not found.", field=fields.pathway_id)
    version, warning = await _selected(
        session, pathway, selector, test_call=test_call, exact=exact, fields=fields
    )
    config = version.global_config
    error_phrase = config.get("errorPhrase")
    ref = PathwayRef(
        pathway_id=pathway.id,
        version=version.version_number,
        call_defaults=pathway_call_defaults(config),
        error_phrase=error_phrase if isinstance(error_phrase, str) and error_phrase.strip() else None,
        start_node_id=_start_node(version, start_node_id, fields),
    )
    return LoadedPathway(ref, () if warning is None else (warning,))
