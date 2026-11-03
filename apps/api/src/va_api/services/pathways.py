"""Pathways API core (specs/12-pathways-api.md §1-§3).

Rules: specs/12 §3 Decision P07-T09a.

Every function runs in the caller's ``tenant_session``. Graph JSON is parsed with
:func:`va_engine.models.load_graph` (schema problems are one ``INVALID_INPUT`` with the field path) and
checked with :func:`va_engine.validation.validate_graph` against the org's live knowledge bases, tools, and
pathways. Save-mode errors reject a write with one ``errors[]`` item per issue (``details.code`` is the
specs/31 §3a detail code). Stored graphs are always :func:`~va_engine.models.dump_graph` output, so the graph
hash is stable.
Parsing and validation run in a worker thread: a graph may be up to 5 MB.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from va_api.audit import audit, changed
from va_api.auth.context import RequestContext
from va_api.auth.pathway_roles import FORCE_DELETE_MESSAGE, can_force_delete
from va_common.errors import AppError, MultiAppError
from va_common.ids import PublicPrefix, to_public
from va_common.settings import Environment
from va_db.models import Pathway, PathwayVersion
from va_db.repositories import PathwayGraphJson, PathwayReference, PathwayRepo
from va_engine.models import PathwayGraph, dump_graph, graph_hash, load_graph, schema_field
from va_engine.validation import (
    GraphIssue,
    GraphRefs,
    Mode,
    ValidationReport,
    validate_graph,
    validation_report,
)

__all__ = [
    "DEFAULT_START_NODE",
    "DRAFT_CHANGED_MESSAGE",
    "FOLDER_NOT_FOUND_MESSAGE",
    "IN_USE_MESSAGE",
    "VERSION_NOT_FOUND_MESSAGE",
    "CheckedGraph",
    "DraftUpdate",
    "GraphInput",
    "VersionSelector",
    "check_graph",
    "create_pathway",
    "delete_pathway",
    "insecure_urls_allowed",
    "load_version",
    "parse_graph",
    "parse_version_selector",
    "reject_errors",
    "update_draft",
    "validate_pathway",
    "validation_errors",
]

DEFAULT_START_NODE: Final[dict[str, Any]] = {
    "id": "start",
    "type": "Default",
    "data": {"name": "Start", "isStart": True, "prompt": "Greet the caller."},
}
DRAFT_CHANGED_MESSAGE: Final = "The draft changed since it was loaded."
FOLDER_NOT_FOUND_MESSAGE: Final = "Folder not found."
IN_USE_MESSAGE: Final = "Pathway is in use."
VERSION_NOT_FOUND_MESSAGE: Final = "Pathway version not found."
INVALID_VERSION_MESSAGE: Final = "version must be a positive integer, 'draft', 'production', or 'staging'."

_INSECURE_ENVS: Final = frozenset({Environment.LOCAL, Environment.TEST})

type VersionSelector = int | Literal["draft", "production", "staging"]


@dataclass(frozen=True, slots=True)
class GraphInput:
    """Graph parts from a request body; ``None`` = not sent (the stored value applies)."""

    nodes: list[Any] | None = None
    edges: list[Any] | None = None
    global_config: dict[str, Any] | None = None

    @classmethod
    def of(cls, stored: PathwayVersion) -> GraphInput:
        """Every graph part of the stored version ``stored``."""
        return cls().over(stored)

    def over(self, stored: PathwayVersion) -> GraphInput:
        """The sent parts with every missing one taken from ``stored``."""
        return GraphInput(
            nodes=self.nodes if self.nodes is not None else list(stored.nodes),
            edges=self.edges if self.edges is not None else list(stored.edges),
            global_config=self.global_config
            if self.global_config is not None
            else dict(stored.global_config),
        )


@dataclass(frozen=True, slots=True)
class CheckedGraph:
    """A parsed graph, its stored JSON, and its validation issues."""

    graph: PathwayGraph
    stored: PathwayGraphJson
    issues: list[GraphIssue]

    @property
    def report(self) -> ValidationReport:
        """The §11 result shape."""
        return validation_report(self.issues)


@dataclass(frozen=True, slots=True)
class DraftUpdate:
    """``POST /v1/pathway/{id}``: only the fields in ``fields_set`` were sent."""

    graph: GraphInput
    name: str | None = None
    description: str | None = None
    folder_id: uuid.UUID | None = None
    expected_graph_hash: str | None = None
    fields_set: frozenset[str] = frozenset()

    def column_values(self) -> dict[str, Any]:
        """Columns to set: sent non-null ``name``/``description`` and a sent ``folder_id``."""
        values: dict[str, Any] = {}
        if "name" in self.fields_set and self.name is not None:
            values["name"] = self.name
        if "description" in self.fields_set and self.description is not None:
            values["description"] = self.description
        if "folder_id" in self.fields_set:
            values["folder_id"] = self.folder_id
        return values


def insecure_urls_allowed(va_env: Environment) -> bool:
    """``http`` and private webhook URLs pass validation only in ``local``/``test``."""
    return va_env in _INSECURE_ENVS


def parse_graph(nodes: Sequence[Any], edges: Sequence[Any], global_config: Mapping[str, Any]) -> PathwayGraph:
    """The graph, or ``INVALID_INPUT`` naming the first schema problem (``nodes[3].data.name``)."""
    try:
        return load_graph(nodes, edges, global_config)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise AppError("INVALID_INPUT", str(first["msg"]), field=schema_field(first["loc"])) from exc


def validation_errors(issues: Sequence[GraphIssue]) -> list[AppError]:
    """One ``INVALID_INPUT`` per error-severity issue, with ``details`` ``{code, node_id?, edge_id?}``."""
    errors: list[AppError] = []
    for issue in issues:
        if issue.severity != "error":
            continue
        details: dict[str, str] = {"code": issue.code}
        if issue.node_id is not None:
            details["node_id"] = issue.node_id
        if issue.edge_id is not None:
            details["edge_id"] = issue.edge_id
        errors.append(AppError("INVALID_INPUT", issue.message, details=details))
    return errors


async def _graph_refs(repo: PathwayRepo, pathway_id: uuid.UUID | None) -> GraphRefs:
    ids = await repo.ref_ids()
    return GraphRefs(
        pathway_id=None if pathway_id is None else str(pathway_id),
        kb_ids=frozenset(
            {to_public(PublicPrefix.KNOWLEDGE_BASE, kb) for kb in ids.kb_ids} | {str(kb) for kb in ids.kb_ids}
        ),
        tool_ids=frozenset(
            {to_public(PublicPrefix.TOOL, tool) for tool in ids.tool_ids}
            | {str(tool) for tool in ids.tool_ids}
        ),
        pathway_ids=frozenset(str(pathway) for pathway in ids.pathway_ids),
    )


def _check_sync(graph_input: GraphInput, refs: GraphRefs, mode: Mode, allow_http: bool) -> CheckedGraph:
    graph = parse_graph(graph_input.nodes or [], graph_input.edges or [], graph_input.global_config or {})
    dumped = dump_graph(graph)
    stored = PathwayGraphJson(
        nodes=dumped["nodes"],
        edges=dumped["edges"],
        global_config=dumped["global_config"],
        graph_hash=graph_hash(dumped),
    )
    issues = validate_graph(graph, mode=mode, refs=refs, allow_http=allow_http)
    return CheckedGraph(graph=graph, stored=stored, issues=issues)


async def check_graph(
    repo: PathwayRepo,
    graph_input: GraphInput,
    *,
    pathway_id: uuid.UUID | None,
    mode: Mode,
    allow_http: bool,
) -> CheckedGraph:
    """Parse and validate ``graph_input`` (all parts set) against the org's references."""
    refs = await _graph_refs(repo, pathway_id)
    return await asyncio.to_thread(_check_sync, graph_input, refs, mode, allow_http)


def reject_errors(checked: CheckedGraph) -> None:
    """``MultiAppError`` with one ``INVALID_INPUT`` item per error-severity issue of ``checked``."""
    errors = validation_errors(checked.issues)
    if errors:
        raise MultiAppError(errors)


async def _check_folder(repo: PathwayRepo, folder_id: uuid.UUID | None) -> None:
    if folder_id is not None and not await repo.folder_exists(folder_id):
        raise AppError("NOT_FOUND", FOLDER_NOT_FOUND_MESSAGE, field="folder_id")


def _summary(pathway: Pathway, draft: PathwayVersion) -> dict[str, Any]:
    """The audited view of a pathway: never the graph itself."""
    return {
        "name": pathway.name,
        "description": pathway.description,
        "folder_id": None if pathway.folder_id is None else str(pathway.folder_id),
        "graph_hash": draft.graph_hash,
    }


async def create_pathway(  # noqa: PLR0913 - keyword-only body fields
    session: AsyncSession,
    ctx: RequestContext,
    *,
    name: str,
    description: str,
    folder_id: uuid.UUID | None,
    graph_input: GraphInput,
    allow_http: bool,
) -> tuple[Pathway, PathwayVersion]:
    """Create a pathway with draft v1; empty ``nodes`` and ``edges`` give the §3 default start node."""
    repo = PathwayRepo(session, ctx.org_id)
    await _check_folder(repo, folder_id)
    if not graph_input.nodes and not graph_input.edges:
        graph_input = GraphInput(
            nodes=[DEFAULT_START_NODE], edges=[], global_config=graph_input.global_config
        )
    checked = await check_graph(repo, graph_input, pathway_id=None, mode="save", allow_http=allow_http)
    reject_errors(checked)
    pathway, draft = await repo.create(
        name=name, description=description, folder_id=folder_id, graph=checked.stored
    )
    await audit(session, ctx, "pathway.create", "pathway", pathway.id, after=_summary(pathway, draft))
    return pathway, draft


def _check_expected_hash(expected: str | None, draft: PathwayVersion) -> None:
    if expected is not None and expected != draft.graph_hash:
        raise AppError(
            "CONFLICT",
            DRAFT_CHANGED_MESSAGE,
            details={"current_graph_hash": draft.graph_hash},
            field="expected_graph_hash",
        )


async def update_draft(
    session: AsyncSession,
    ctx: RequestContext,
    pathway_id: uuid.UUID,
    change: DraftUpdate,
    *,
    allow_http: bool,
) -> tuple[Pathway, PathwayVersion, ValidationReport]:
    """Change the draft under the pathway row lock; ``CONFLICT`` when ``expected_graph_hash`` is stale."""
    repo = PathwayRepo(session, ctx.org_id)
    pathway = await repo.get(pathway_id, lock=True)
    draft = await repo.draft(pathway)
    _check_expected_hash(change.expected_graph_hash, draft)
    checked = await check_graph(
        repo, change.graph.over(draft), pathway_id=pathway.id, mode="save", allow_http=allow_http
    )
    reject_errors(checked)
    values = change.column_values()
    if "folder_id" in values:
        await _check_folder(repo, change.folder_id)
    before = _summary(pathway, draft)
    graph = checked.stored if checked.stored.graph_hash != draft.graph_hash else None
    if values or graph is not None:
        await repo.update_draft(pathway, draft, values=values, graph=graph)
    old, new = changed(before, _summary(pathway, draft))
    if old or new:
        await audit(session, ctx, "pathway.update", "pathway", pathway.id, before=old, after=new)
    return pathway, draft, checked.report


async def validate_pathway(  # noqa: PLR0913 - keyword-only mode and URL policy
    session: AsyncSession,
    ctx: RequestContext,
    pathway_id: uuid.UUID,
    graph_input: GraphInput,
    *,
    mode: Mode,
    allow_http: bool,
) -> ValidationReport:
    """Validate the supplied graph (missing parts from the draft) or, when nothing is supplied, the draft."""
    repo = PathwayRepo(session, ctx.org_id)
    pathway = await repo.get(pathway_id)
    draft = await repo.draft(pathway)
    checked = await check_graph(
        repo, graph_input.over(draft), pathway_id=pathway.id, mode=mode, allow_http=allow_http
    )
    return checked.report


def _reference_item(reference: PathwayReference) -> dict[str, str]:
    return {"type": reference.type, "id": str(reference.id), "name": reference.name}


async def delete_pathway(
    session: AsyncSession, ctx: RequestContext, pathway_id: uuid.UUID, *, force: bool
) -> list[PathwayReference]:
    """Soft delete; ``CONFLICT`` with ``details.references`` while in use, unless an owner/admin forces it."""
    if force and not can_force_delete(ctx):
        raise AppError("FORBIDDEN", FORCE_DELETE_MESSAGE)
    repo = PathwayRepo(session, ctx.org_id)
    pathway = await repo.get(pathway_id, lock=True)
    references = await repo.references(pathway.id)
    if references and not force:
        raise AppError(
            "CONFLICT", IN_USE_MESSAGE, details={"references": [_reference_item(ref) for ref in references]}
        )
    await repo.soft_delete(pathway)
    after = {"force": force, "references": len(references)}
    await audit(session, ctx, "pathway.delete", "pathway", pathway.id, after=after)
    return references


def parse_version_selector(value: str | None) -> VersionSelector:
    """``?version=``: a positive integer, ``draft`` (default), ``production``, or ``staging``."""
    if value is None or value == "draft":
        return "draft"
    if value in {"production", "staging"}:
        return "production" if value == "production" else "staging"
    if value.isascii() and value.isdigit() and int(value) >= 1:
        return int(value)
    raise AppError("INVALID_INPUT", INVALID_VERSION_MESSAGE, field="version")


async def load_version(
    session: AsyncSession, ctx: RequestContext, pathway_id: uuid.UUID, selector: VersionSelector
) -> tuple[Pathway, PathwayVersion]:
    """The pathway and the selected version; ``PATHWAY_VERSION_NOT_FOUND`` for an unset pin or number."""
    repo = PathwayRepo(session, ctx.org_id)
    pathway = await repo.get(pathway_id)
    number: int | None
    if selector == "draft":
        number = pathway.draft_version
    elif selector == "production":
        number = pathway.production_version
    elif selector == "staging":
        number = pathway.staging_version
    else:
        number = selector
    version = None if number is None else await repo.version(pathway, number)
    if version is None:
        raise AppError("PATHWAY_VERSION_NOT_FOUND", VERSION_NOT_FOUND_MESSAGE, field="version")
    return pathway, version
