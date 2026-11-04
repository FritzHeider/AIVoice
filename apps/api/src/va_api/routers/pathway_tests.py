"""Pathway unit tests and test runs under ``/v1/pathway/{pathway_id}`` (specs/12-pathways-api.md §8; legacy).

Rules: specs/12 §8 Decision P07-T11b. The routes attach to :data:`va_api.routers.pathways.pathways_router`
(``va_api.main`` imports this module for that side effect). Reads need ``pathways:read``; writes need
``pathways:write`` with no editing-role guard: specs/04 §3.2 lets analysts manage pathway tests. The literal
``/tests/from_call`` route is declared before ``/tests/{test_id}``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

from fastapi import Depends, Query, Request

from va_api.audit import audited
from va_api.auth.context import RequestContext
from va_api.auth.dependencies import require_scope
from va_api.auth.session_auth import database_of
from va_api.routers.pathway_tests_schemas import (
    RunList,
    RunOne,
    UnitTestCreate,
    UnitTestDeleted,
    UnitTestFromCall,
    UnitTestList,
    UnitTestOne,
    UnitTestUpdate,
    run_detail,
    run_item,
    unit_test_view,
)
from va_api.routers.pathways import DEFAULT_LIST_LIMIT, pathways_router
from va_api.services.pathway_tests import (
    TEST_DELETED_MESSAGE,
    capture_from_call,
    create_unit_test,
    delete_unit_test,
    get_run,
    get_unit_test,
    list_runs,
    list_unit_tests,
    update_unit_test,
)
from va_db.models import PathwayTestRunKind

__all__ = [
    "pathways_runs_get",
    "pathways_runs_list",
    "pathways_tests_create",
    "pathways_tests_delete",
    "pathways_tests_from_call",
    "pathways_tests_get",
    "pathways_tests_list",
    "pathways_tests_update",
]

ReadPathways = Annotated[RequestContext, Depends(require_scope("pathways:read"))]
WritePathways = Annotated[RequestContext, Depends(require_scope("pathways:write"))]
Limit = Annotated[int, Query(ge=1, le=100, description="Page size (1-100).")]
Cursor = Annotated[str | None, Query(description="next_cursor of the previous page.")]
_READ: Final = {"x-scope": "pathways:read"}
_WRITE: Final = {"x-scope": "pathways:write"}


@pathways_router.get("/{pathway_id}/tests", operation_id="pathways_tests_list", openapi_extra=_READ)
async def pathways_tests_list(
    pathway_id: uuid.UUID,
    request: Request,
    ctx: ReadPathways,
    limit: Limit = DEFAULT_LIST_LIMIT,
    cursor: Cursor = None,
) -> UnitTestList:
    """The pathway's unit tests, newest first."""
    async with database_of(request).tenant_session(ctx.org_id) as session:
        page = await list_unit_tests(session, ctx, pathway_id, limit=limit, cursor=cursor)
        return UnitTestList(tests=[unit_test_view(row) for row in page.items], next_cursor=page.next_cursor)


@pathways_router.post(
    "/{pathway_id}/tests",
    operation_id="pathways_tests_create",
    openapi_extra={**_WRITE, **audited("pathway_test.create")},
)
async def pathways_tests_create(
    pathway_id: uuid.UUID, body: UnitTestCreate, request: Request, ctx: WritePathways
) -> UnitTestOne:
    """Create a unit test: the caller says ``user_message`` on ``node_id`` after ``history``."""
    values = {
        "name": body.name,
        "node_id": body.node_id,
        "history": [item.model_dump() for item in body.history],
        "user_message": body.user_message,
        "expected": body.expected.stored(),
        "simulated_variations": body.simulated_variations,
    }
    async with database_of(request).tenant_session(ctx.org_id) as session:
        test = await create_unit_test(session, ctx, pathway_id, values)
        return UnitTestOne(test=unit_test_view(test))


@pathways_router.post(
    "/{pathway_id}/tests/from_call",
    operation_id="pathways_tests_from_call",
    openapi_extra={**_WRITE, **audited("pathway_test.create")},
)
async def pathways_tests_from_call(
    pathway_id: uuid.UUID, body: UnitTestFromCall, request: Request, ctx: WritePathways
) -> UnitTestOne:
    """Create a unit test from a caller turn of a call that ran this pathway."""
    async with database_of(request).tenant_session(ctx.org_id) as session:
        test = await capture_from_call(
            session,
            ctx,
            pathway_id,
            call_id=body.call_id,
            turn_index=body.turn_index,
            name=body.name,
            expected=None if body.expected is None else body.expected.stored(),
            simulated_variations=body.simulated_variations,
        )
        return UnitTestOne(test=unit_test_view(test))


@pathways_router.get("/{pathway_id}/tests/{test_id}", operation_id="pathways_tests_get", openapi_extra=_READ)
async def pathways_tests_get(
    pathway_id: uuid.UUID, test_id: uuid.UUID, request: Request, ctx: ReadPathways
) -> UnitTestOne:
    """One unit test of the pathway."""
    async with database_of(request).tenant_session(ctx.org_id) as session:
        return UnitTestOne(test=unit_test_view(await get_unit_test(session, ctx, pathway_id, test_id)))


@pathways_router.patch(
    "/{pathway_id}/tests/{test_id}",
    operation_id="pathways_tests_update",
    openapi_extra={**_WRITE, **audited("pathway_test.update")},
)
async def pathways_tests_update(
    pathway_id: uuid.UUID, test_id: uuid.UUID, body: UnitTestUpdate, request: Request, ctx: WritePathways
) -> UnitTestOne:
    """Change a unit test; sent non-null fields replace the stored ones (``expected`` as a whole)."""
    async with database_of(request).tenant_session(ctx.org_id) as session:
        test = await update_unit_test(session, ctx, pathway_id, test_id, body.values())
        return UnitTestOne(test=unit_test_view(test))


@pathways_router.delete(
    "/{pathway_id}/tests/{test_id}",
    operation_id="pathways_tests_delete",
    openapi_extra={**_WRITE, **audited("pathway_test.delete")},
)
async def pathways_tests_delete(
    pathway_id: uuid.UUID, test_id: uuid.UUID, request: Request, ctx: WritePathways
) -> UnitTestDeleted:
    """Delete a unit test (past run results keep their copy)."""
    async with database_of(request).tenant_session(ctx.org_id) as session:
        await delete_unit_test(session, ctx, pathway_id, test_id)
    return UnitTestDeleted(message=TEST_DELETED_MESSAGE)


@pathways_router.get("/{pathway_id}/runs", operation_id="pathways_runs_list", openapi_extra=_READ)
async def pathways_runs_list(
    *,
    pathway_id: uuid.UUID,
    request: Request,
    ctx: ReadPathways,
    kind: Annotated[PathwayTestRunKind | None, Query(description="Only runs of this kind.")] = None,
    limit: Limit = DEFAULT_LIST_LIMIT,
    cursor: Cursor = None,
) -> RunList:
    """The pathway's unit, simulation, and regression runs, newest first (without results)."""
    async with database_of(request).tenant_session(ctx.org_id) as session:
        page = await list_runs(session, ctx, pathway_id, kind=kind, limit=limit, cursor=cursor)
        return RunList(runs=[run_item(row) for row in page.items], next_cursor=page.next_cursor)


@pathways_router.get("/{pathway_id}/runs/{run_id}", operation_id="pathways_runs_get", openapi_extra=_READ)
async def pathways_runs_get(
    pathway_id: uuid.UUID, run_id: uuid.UUID, request: Request, ctx: ReadPathways
) -> RunOne:
    """One run of the pathway with its results."""
    async with database_of(request).tenant_session(ctx.org_id) as session:
        return RunOne(run=run_detail(await get_run(session, ctx, pathway_id, run_id)))
