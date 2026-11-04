"""Pathway versions under ``/v1/pathway/{pathway_id}`` (specs/12-pathways-api.md §4; legacy bodies).

Rules: specs/12 §4 Decision P07-T09b. The routes attach to :data:`va_api.routers.pathways.pathways_router`
(``va_api.main`` imports this module for that side effect). Reads need ``pathways:read``; saving a version
needs an editing role; publish, promote, and revert need ``pathways.publish`` for session principals
(:func:`~va_api.auth.pathway_roles.require_pathway_publisher`). The resolved graph cache
``pathway:{id}:v:{env}`` is invalidated after the transaction commits (a ``pending_regression`` publish is
invalidated by the worker when it completes; Decision P07-T11e2).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

from fastapi import Depends, Path, Request

from va_api.audit import audited
from va_api.auth.context import RequestContext
from va_api.auth.dependencies import require_scope
from va_api.auth.pathway_roles import PUBLISH_PERMISSION, require_pathway_editor, require_pathway_publisher
from va_api.auth.session_auth import clock_of, database_of, redis_of, settings_of
from va_api.routers.pathway_versions_schemas import (
    PathwayVersions,
    Promoted,
    PublishBody,
    Published,
    RevertBody,
    Reverted,
    VersionSave,
    VersionSaved,
    version_item,
)
from va_api.routers.pathways import pathways_router
from va_api.routers.pathways_schemas import PathwayDetail, pathway_detail, validation_view
from va_api.services.pathway_publish import PublishDeps, PublishRequest, publish_pathway
from va_api.services.pathway_regression_sync import RegressionTestRunner
from va_api.services.pathway_test_runs import PathwayTestStarter
from va_api.services.pathway_versions import (
    get_version,
    list_versions,
    promote_staging,
    revert_draft,
    save_version,
)
from va_api.services.pathways import insecure_urls_allowed
from va_db.pathway_cache import invalidate_pathway_cache

__all__ = [
    "pathways_promote",
    "pathways_publish",
    "pathways_revert",
    "pathways_version_get",
    "pathways_version_save",
    "pathways_versions_list",
]

ReadPathways = Annotated[RequestContext, Depends(require_scope("pathways:read"))]
WritePathways = Annotated[RequestContext, Depends(require_scope("pathways:write"))]
VersionNumberPath = Annotated[int, Path(ge=1, description="Version number (1 or more).")]
_EDITOR: Final = [Depends(require_pathway_editor)]
_PUBLISHER: Final = [Depends(require_pathway_publisher)]
_READ_SCOPE: Final = {"x-scope": "pathways:read"}
_PUBLISH: Final = {"x-scope": "pathways:write", "x-permission": PUBLISH_PERMISSION}


@pathways_router.get(
    "/{pathway_id}/versions", operation_id="pathways_versions_list", openapi_extra=_READ_SCOPE
)
async def pathways_versions_list(
    pathway_id: uuid.UUID, request: Request, ctx: ReadPathways
) -> PathwayVersions:
    """Every version of the pathway, newest first, with its environment pins."""
    async with database_of(request).tenant_session(ctx.org_id) as session:
        pathway, versions = await list_versions(session, ctx, pathway_id)
        return PathwayVersions(versions=[version_item(pathway, version) for version in versions])


@pathways_router.get(
    "/{pathway_id}/version/{version_number}", operation_id="pathways_version_get", openapi_extra=_READ_SCOPE
)
async def pathways_version_get(
    pathway_id: uuid.UUID, version_number: VersionNumberPath, request: Request, ctx: ReadPathways
) -> PathwayDetail:
    """The pathway with the graph of one version (same body as ``GET /v1/pathway/{id}?version=n``)."""
    async with database_of(request).tenant_session(ctx.org_id) as session:
        pathway, version = await get_version(session, ctx, pathway_id, version_number)
        return pathway_detail(pathway, version)


@pathways_router.post(
    "/{pathway_id}/version",
    operation_id="pathways_version_save",
    dependencies=_EDITOR,
    openapi_extra={"x-scope": "pathways:write", **audited("pathway.version_save")},
)
async def pathways_version_save(
    pathway_id: uuid.UUID, request: Request, ctx: WritePathways, body: VersionSave | None = None
) -> VersionSaved:
    """Freeze the draft as a saved version and open the next draft."""
    name = body.name if body is not None else None
    async with database_of(request).tenant_session(ctx.org_id) as session:
        outcome = await save_version(session, ctx, pathway_id, name)
    return VersionSaved(saved_version=outcome.saved_version, draft_version=outcome.draft_version)


@pathways_router.post(
    "/{pathway_id}/publish",
    operation_id="pathways_publish",
    dependencies=_PUBLISHER,
    openapi_extra={**_PUBLISH, **audited("pathway.publish")},
)
async def pathways_publish(
    pathway_id: uuid.UUID, request: Request, ctx: WritePathways, body: PublishBody | None = None
) -> Published:
    """Validate a version in publish mode and pin it to production or staging, gated by the regression suite
    when it runs: a blocking suite of at most 20 unit tests runs in the request (409 ``CONFLICT`` with
    ``details.run_id`` when it does not pass), a larger one answers ``pending_regression`` and the worker
    publishes once the run passes."""
    payload = body if body is not None else PublishBody()
    container = request.app.state.container
    starter: PathwayTestStarter = container.pathway_test_starter
    runner: RegressionTestRunner = container.pathway_test_runner
    outcome = await publish_pathway(
        PublishDeps(database=database_of(request), starter=starter, runner=runner, clock=clock_of(request)),
        ctx,
        pathway_id,
        PublishRequest(
            version_number=payload.version_number,
            environment=payload.environment,
            run_regression=payload.run_regression,
            allow_http=insecure_urls_allowed(settings_of(request).va_env),
        ),
    )
    if outcome.status == "success":
        await invalidate_pathway_cache(redis_of(request), pathway_id, [outcome.environment])
    return Published(
        status=outcome.status,
        published_version=outcome.published_version,
        environment=outcome.environment,
        version_number=outcome.version_number,
        draft_version=outcome.draft_version,
        run_id=outcome.run_id,
    )


@pathways_router.post(
    "/{pathway_id}/promote",
    operation_id="pathways_promote",
    dependencies=_PUBLISHER,
    openapi_extra={**_PUBLISH, **audited("pathway.promote")},
)
async def pathways_promote(pathway_id: uuid.UUID, request: Request, ctx: WritePathways) -> Promoted:
    """Pin the staging version to production."""
    async with database_of(request).tenant_session(ctx.org_id) as session:
        outcome = await promote_staging(session, ctx, pathway_id)
    if outcome.moved:
        await invalidate_pathway_cache(redis_of(request), pathway_id, ["production"])
    return Promoted(production_version=outcome.production_version)


@pathways_router.post(
    "/{pathway_id}/revert",
    operation_id="pathways_revert",
    dependencies=_PUBLISHER,
    openapi_extra={**_PUBLISH, **audited("pathway.revert")},
)
async def pathways_revert(
    pathway_id: uuid.UUID, body: RevertBody, request: Request, ctx: WritePathways
) -> Reverted:
    """Replace the draft graph with a copy of an earlier version."""
    async with database_of(request).tenant_session(ctx.org_id) as session:
        outcome = await revert_draft(
            session,
            ctx,
            pathway_id,
            body.version_number,
            allow_http=insecure_urls_allowed(settings_of(request).va_env),
        )
        return Reverted(
            draft_version=outcome.draft.version_number,
            graph_hash=outcome.draft.graph_hash,
            validation=validation_view(outcome.report),
        )
