"""Pathway regression suite settings under ``/v1/pathway/{pathway_id}/regression`` (specs/12-pathways-api.md
§9; legacy bodies).

Rules: specs/12 §9 Decision P07-T11e1. The routes attach to :data:`va_api.routers.pathways.pathways_router`
(``va_api.main`` imports this module for that side effect). Reading needs ``pathways:read``; writing edits the
draft, so it needs ``pathways:write`` and an editing role for session principals.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

from fastapi import Depends, Request

from va_api.audit import audited
from va_api.auth.context import RequestContext
from va_api.auth.dependencies import require_scope
from va_api.auth.pathway_roles import require_pathway_editor
from va_api.auth.session_auth import database_of
from va_api.routers.pathway_regression_schemas import RegressionBody, RegressionSaved, RegressionView
from va_api.routers.pathways import pathways_router
from va_api.services.pathway_regression import get_regression, put_regression
from va_domain.pathway_tests import RegressionSettings

__all__ = ["pathways_regression_get", "pathways_regression_put"]

ReadPathways = Annotated[RequestContext, Depends(require_scope("pathways:read"))]
WritePathways = Annotated[RequestContext, Depends(require_scope("pathways:write"))]
_EDITOR: Final = [Depends(require_pathway_editor)]


@pathways_router.get(
    "/{pathway_id}/regression",
    operation_id="pathways_regression_get",
    openapi_extra={"x-scope": "pathways:read"},
)
async def pathways_regression_get(
    pathway_id: uuid.UUID, request: Request, ctx: ReadPathways
) -> RegressionView:
    """The regression suite settings stored in the pathway's draft."""
    async with database_of(request).tenant_session(ctx.org_id) as session:
        settings = await get_regression(session, ctx, pathway_id)
    return RegressionView(regression=settings)


@pathways_router.put(
    "/{pathway_id}/regression",
    operation_id="pathways_regression_put",
    dependencies=_EDITOR,
    openapi_extra={"x-scope": "pathways:write", **audited("pathway.regression_update")},
)
async def pathways_regression_put(
    pathway_id: uuid.UUID, body: RegressionBody, request: Request, ctx: WritePathways
) -> RegressionSaved:
    """Replace the regression suite settings of the pathway's draft (omitted keys take their defaults)."""
    settings = RegressionSettings.model_validate(body.model_dump())
    async with database_of(request).tenant_session(ctx.org_id) as session:
        outcome = await put_regression(session, ctx, pathway_id, settings)
        return RegressionSaved(
            regression=outcome.settings,
            draft_version=outcome.draft.version_number,
            graph_hash=outcome.draft.graph_hash,
        )
