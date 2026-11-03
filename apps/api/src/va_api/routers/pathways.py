"""Conversational Pathways API core under ``/v1/pathway`` (specs/12-pathways-api.md §2, §3; legacy bodies).

Rules: specs/12 §3 Decision P07-T09a. Reads need ``pathways:read``; changes need ``pathways:write`` and, for
session principals, an editing role (:func:`va_api.auth.pathway_roles.require_pathway_editor`: analysts hold
``pathways:write`` for tests and chat only). Graphs are validated in save mode on every write, and each
error-severity issue becomes one ``INVALID_INPUT`` item (``details.code`` = the specs/31 §3a detail code).
The draft update takes the pathway row lock, so concurrent edits with the same ``expected_graph_hash``
serialise and the second one gets ``CONFLICT``. The literal ``/create`` route is declared before the
``/{pathway_id}`` routes.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

from fastapi import Depends, Query, Request

from va_api.audit import audit_exempt, audited
from va_api.auth.context import RequestContext
from va_api.auth.dependencies import require_scope
from va_api.auth.pathway_roles import require_pathway_editor
from va_api.auth.session_auth import database_of, settings_of
from va_api.envelopes import legacy_router
from va_api.routers.pathways_schemas import (
    PathwayCreate,
    PathwayCreated,
    PathwayDeleted,
    PathwayDetail,
    PathwayList,
    PathwayUpdate,
    PathwayUpdated,
    PathwayValidate,
    PathwayValidated,
    pathway_detail,
    pathway_list_item,
    validation_view,
)
from va_api.services.pathways import (
    DraftUpdate,
    GraphInput,
    create_pathway,
    delete_pathway,
    insecure_urls_allowed,
    load_version,
    parse_version_selector,
    update_draft,
    validate_pathway,
)
from va_db.repositories import PathwayRepo

__all__ = [
    "DEFAULT_LIST_LIMIT",
    "DELETED_MESSAGE",
    "pathways_create",
    "pathways_delete",
    "pathways_get",
    "pathways_list",
    "pathways_router",
    "pathways_update",
    "pathways_validate",
]

DEFAULT_LIST_LIMIT: Final = 50
DELETED_MESSAGE: Final = "Pathway deleted."
_MAX_SEARCH_LENGTH: Final = 200

pathways_router = legacy_router(prefix="/v1/pathway", tags=["Pathways"])

ReadPathways = Annotated[RequestContext, Depends(require_scope("pathways:read"))]
WritePathways = Annotated[RequestContext, Depends(require_scope("pathways:write"))]
_EDITOR: Final = [Depends(require_pathway_editor)]
_READ_SCOPE: Final = {"x-scope": "pathways:read"}
_WRITE_SCOPE: Final = {"x-scope": "pathways:write"}


def _allow_http(request: Request) -> bool:
    return insecure_urls_allowed(settings_of(request).va_env)


@pathways_router.post(
    "/create",
    operation_id="pathways_create",
    dependencies=_EDITOR,
    openapi_extra={**_WRITE_SCOPE, **audited("pathway.create")},
)
async def pathways_create(body: PathwayCreate, request: Request, ctx: WritePathways) -> PathwayCreated:
    """Create a pathway and its draft version 1 (a single start node when no graph is given)."""
    graph = GraphInput(nodes=body.nodes, edges=body.edges, global_config=body.global_config)
    async with database_of(request).tenant_session(ctx.org_id) as session:
        pathway, draft = await create_pathway(
            session,
            ctx,
            name=body.name,
            description=body.description or "",
            folder_id=body.folder_id,
            graph_input=graph,
            allow_http=_allow_http(request),
        )
        return PathwayCreated(pathway_id=pathway.id, version_number=draft.version_number)


@pathways_router.get("", operation_id="pathways_list", openapi_extra=_READ_SCOPE)
async def pathways_list(
    *,
    request: Request,
    ctx: ReadPathways,
    folder_id: Annotated[uuid.UUID | None, Query(description="Only pathways in this folder.")] = None,
    search: Annotated[
        str | None, Query(max_length=_MAX_SEARCH_LENGTH, description="Case-insensitive name substring.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="Page size (1-100).")] = DEFAULT_LIST_LIMIT,
    cursor: Annotated[str | None, Query(description="next_cursor of the previous page.")] = None,
) -> PathwayList:
    """The org's live pathways, newest first."""
    async with database_of(request).tenant_session(ctx.org_id) as session:
        page = await PathwayRepo(session, ctx.org_id).page(
            limit=limit, cursor=cursor, folder_id=folder_id, search=search
        )
        return PathwayList(
            pathways=[pathway_list_item(row) for row in page.items], next_cursor=page.next_cursor
        )


@pathways_router.get("/{pathway_id}", operation_id="pathways_get", openapi_extra=_READ_SCOPE)
async def pathways_get(
    pathway_id: uuid.UUID,
    request: Request,
    ctx: ReadPathways,
    version: Annotated[
        str | None, Query(description="Version number, `draft` (default), `production`, or `staging`.")
    ] = None,
) -> PathwayDetail:
    """One pathway with the graph of the selected version."""
    selector = parse_version_selector(version)
    async with database_of(request).tenant_session(ctx.org_id) as session:
        pathway, selected = await load_version(session, ctx, pathway_id, selector)
        return pathway_detail(pathway, selected)


@pathways_router.post(
    "/{pathway_id}",
    operation_id="pathways_update",
    dependencies=_EDITOR,
    openapi_extra={**_WRITE_SCOPE, **audited("pathway.update")},
)
async def pathways_update(
    pathway_id: uuid.UUID, body: PathwayUpdate, request: Request, ctx: WritePathways
) -> PathwayUpdated:
    """Update the draft; graph parts replace the stored ones and are validated in save mode."""
    change = DraftUpdate(
        graph=GraphInput(nodes=body.nodes, edges=body.edges, global_config=body.global_config),
        name=body.name,
        description=body.description,
        folder_id=body.folder_id,
        expected_graph_hash=body.expected_graph_hash,
        fields_set=frozenset(body.model_fields_set),
    )
    async with database_of(request).tenant_session(ctx.org_id) as session:
        pathway, draft, report = await update_draft(
            session, ctx, pathway_id, change, allow_http=_allow_http(request)
        )
        return PathwayUpdated(
            pathway_id=pathway.id,
            version_number=draft.version_number,
            graph_hash=draft.graph_hash,
            validation=validation_view(report),
        )


@pathways_router.delete(
    "/{pathway_id}",
    operation_id="pathways_delete",
    dependencies=_EDITOR,
    openapi_extra={**_WRITE_SCOPE, **audited("pathway.delete")},
)
async def pathways_delete(
    pathway_id: uuid.UUID,
    request: Request,
    ctx: WritePathways,
    force: Annotated[
        bool, Query(description="Delete even while referenced (owner/admin sessions, `*` API keys).")
    ] = False,
) -> PathwayDeleted:
    """Soft-delete a pathway that nothing live references (or any pathway with ``force``)."""
    async with database_of(request).tenant_session(ctx.org_id) as session:
        await delete_pathway(session, ctx, pathway_id, force=force)
    return PathwayDeleted(message=DELETED_MESSAGE)


@pathways_router.post(
    "/{pathway_id}/validate",
    operation_id="pathways_validate",
    dependencies=_EDITOR,
    openapi_extra={**_WRITE_SCOPE, **audit_exempt("read-only check")},
)
async def pathways_validate(
    pathway_id: uuid.UUID, request: Request, ctx: WritePathways, body: PathwayValidate | None = None
) -> PathwayValidated:
    """Validate the draft or a supplied graph; issues are returned, never raised."""
    payload = body if body is not None else PathwayValidate()
    graph = GraphInput(nodes=payload.nodes, edges=payload.edges, global_config=payload.global_config)
    async with database_of(request).tenant_session(ctx.org_id) as session:
        report = await validate_pathway(
            session, ctx, pathway_id, graph, mode=payload.mode, allow_http=_allow_http(request)
        )
        return PathwayValidated(validation=validation_view(report))
