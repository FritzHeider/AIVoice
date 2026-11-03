"""Pathway import, export, and duplicate under ``/v1/pathway`` (specs/12-pathways-api.md §4, §5).

Legacy bodies; rules: specs/12 §6 Decision P07-T09c. :data:`va_api.main.ROUTERS` lists this router before
the pathways router, so ``POST /v1/pathway/import`` is never taken by ``POST /v1/pathway/{pathway_id}``.
Export needs ``pathways:read``; import and duplicate need ``pathways:write`` and an editing role.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Query, Request

from va_api.audit import audited
from va_api.auth.session_auth import clock_of, database_of
from va_api.envelopes import legacy_router
from va_api.routers.pathway_io_schemas import DuplicateBody, ExportBody, ImportBody, Imported, export_body
from va_api.routers.pathways import EDITOR, WRITE_SCOPE, ReadPathways, WritePathways, allow_http
from va_api.routers.pathways_schemas import PathwayCreated, validation_view
from va_api.services.pathway_io import (
    PathwayImport,
    duplicate_pathway,
    export_pathway,
    import_pathway,
    missing_reference_warnings,
)
from va_api.services.pathways import GraphInput, parse_version_selector

__all__ = ["pathway_io_router", "pathways_duplicate", "pathways_export", "pathways_import"]

pathway_io_router = legacy_router(prefix="/v1/pathway", tags=["Pathways"])


@pathway_io_router.post(
    "/import",
    operation_id="pathways_import",
    dependencies=EDITOR,
    openapi_extra={**WRITE_SCOPE, **audited("pathway.import")},
)
async def pathways_import(body: ImportBody, request: Request, ctx: WritePathways) -> Imported:
    """Create a pathway from our export or a Bland export; missing tools/KBs are kept and reported."""
    document = PathwayImport(
        graph=GraphInput(nodes=body.nodes, edges=body.edges, global_config=body.global_config or {}),
        name=body.name,
        description=body.description,
        folder_id=body.folder_id,
        format=body.format,
    )
    async with database_of(request).tenant_session(ctx.org_id) as session:
        created = await import_pathway(session, ctx, document, allow_http=allow_http(request))
        view = validation_view(created.report)
        return Imported(
            pathway_id=created.pathway.id,
            version_number=created.draft.version_number,
            validation=view,
            warnings=[
                {key: str(value) for key, value in item.items()}
                for item in missing_reference_warnings(created.report)
            ],
        )


@pathway_io_router.get(
    "/{pathway_id}/export", operation_id="pathways_export", openapi_extra={"x-scope": "pathways:read"}
)
async def pathways_export(
    pathway_id: uuid.UUID,
    request: Request,
    ctx: ReadPathways,
    version: Annotated[
        str | None, Query(description="Version number, `draft` (default), `production`, or `staging`.")
    ] = None,
) -> ExportBody:
    """The ``voiceagents.pathway.v1`` document of the selected version."""
    selector = parse_version_selector(version)
    async with database_of(request).tenant_session(ctx.org_id) as session:
        document = await export_pathway(session, ctx, pathway_id, selector, now=clock_of(request).now())
    return export_body(document)


@pathway_io_router.post(
    "/{pathway_id}/duplicate",
    operation_id="pathways_duplicate",
    dependencies=EDITOR,
    openapi_extra={**WRITE_SCOPE, **audited("pathway.duplicate")},
)
async def pathways_duplicate(
    pathway_id: uuid.UUID, request: Request, ctx: WritePathways, body: DuplicateBody | None = None
) -> PathwayCreated:
    """Copy one version of the pathway into a new pathway (draft v1)."""
    payload = body if body is not None else DuplicateBody()
    async with database_of(request).tenant_session(ctx.org_id) as session:
        created = await duplicate_pathway(
            session,
            ctx,
            pathway_id,
            name=payload.name,
            selector=payload.version,
            allow_http=allow_http(request),
        )
        return PathwayCreated(pathway_id=created.pathway.id, version_number=created.draft.version_number)
