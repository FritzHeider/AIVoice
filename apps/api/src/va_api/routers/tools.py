"""Custom tools API under ``/v1/tools`` (specs/13-tools-and-dynamic-data.md §2; legacy bodies).

Rules: specs/13 §2 Decision P08-T01. Reads need ``tools:read``, changes ``tools:write``. ``tool_id`` accepts
``TL-<uuid>`` or the bare uuid; responses always use the ``TL-`` form and mask secret header values. The
``/test`` route arrives with the tool runtime (P08-T02).
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import Depends, Query, Request

from va_api.audit import audited
from va_api.auth.context import RequestContext
from va_api.auth.dependencies import require_scope
from va_api.auth.session_auth import database_of, envelope_of, settings_of
from va_api.envelopes import legacy_router
from va_api.routers.tools_schemas import (
    ToolCreate,
    ToolCreated,
    ToolDeleted,
    ToolEnvelope,
    ToolList,
    ToolUpdate,
)
from va_api.services.tools import (
    DELETED_MESSAGE,
    ToolDeps,
    create_tool,
    delete_tool,
    get_tool,
    list_tools,
    parse_tool_id,
    update_tool,
)
from va_pathways.runs import insecure_urls_allowed

__all__ = [
    "DEFAULT_LIST_LIMIT",
    "tool_deps",
    "tools_create",
    "tools_delete",
    "tools_get",
    "tools_list",
    "tools_router",
    "tools_update",
]

DEFAULT_LIST_LIMIT: Final = 50
_MAX_SEARCH_LENGTH: Final = 200
_TOOL_ID_DESCRIPTION: Final = "`TL-<uuid>` or the bare uuid."

tools_router = legacy_router(prefix="/v1/tools", tags=["Tools"])

ReadTools = Annotated[RequestContext, Depends(require_scope("tools:read"))]
WriteTools = Annotated[RequestContext, Depends(require_scope("tools:write"))]
_READ: Final = {"x-scope": "tools:read"}
_WRITE: Final = {"x-scope": "tools:write"}


def tool_deps(request: Request) -> ToolDeps:
    """The service dependencies of a request (``http`` URLs pass only in ``local``/``test``)."""
    return ToolDeps(
        database=database_of(request),
        envelope=envelope_of(request),
        allow_http=insecure_urls_allowed(settings_of(request).va_env),
    )


@tools_router.post("", operation_id="tools_create", openapi_extra={**_WRITE, **audited("tool.create")})
async def tools_create(body: ToolCreate, request: Request, ctx: WriteTools) -> ToolCreated:
    """Create a tool; secret header values are stored encrypted and never returned."""
    view = await create_tool(tool_deps(request), ctx, body)
    return ToolCreated(tool_id=view.tool_id, tool=view)


@tools_router.get("", operation_id="tools_list", openapi_extra=_READ)
async def tools_list(
    *,
    request: Request,
    ctx: ReadTools,
    search: Annotated[
        str | None,
        Query(max_length=_MAX_SEARCH_LENGTH, description="Case-insensitive name or description substring."),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="Page size (1-100).")] = DEFAULT_LIST_LIMIT,
    cursor: Annotated[str | None, Query(description="next_cursor of the previous page.")] = None,
) -> ToolList:
    """The org's live tools, newest first."""
    page = await list_tools(tool_deps(request), ctx, limit=limit, cursor=cursor, search=search)
    return ToolList(tools=page.tools, next_cursor=page.next_cursor)


@tools_router.get("/{tool_id}", operation_id="tools_get", openapi_extra=_READ)
async def tools_get(tool_id: str, request: Request, ctx: ReadTools) -> ToolEnvelope:
    """One tool."""
    view = await get_tool(tool_deps(request), ctx, parse_tool_id(tool_id))
    return ToolEnvelope(tool=view)


@tools_router.post(
    "/{tool_id}", operation_id="tools_update", openapi_extra={**_WRITE, **audited("tool.update")}
)
async def tools_update(tool_id: str, body: ToolUpdate, request: Request, ctx: WriteTools) -> ToolEnvelope:
    """Update any subset of the fields; the merged definition is validated as a whole."""
    view = await update_tool(tool_deps(request), ctx, parse_tool_id(tool_id), body)
    return ToolEnvelope(tool=view)


@tools_router.delete(
    "/{tool_id}", operation_id="tools_delete", openapi_extra={**_WRITE, **audited("tool.delete")}
)
async def tools_delete(
    tool_id: str,
    request: Request,
    ctx: WriteTools,
    force: Annotated[
        bool, Query(description="Delete even while published pathways, personas, or agents list the tool.")
    ] = False,
) -> ToolDeleted:
    """Soft-delete a tool that no published resource lists (or any tool with ``force``)."""
    await delete_tool(tool_deps(request), ctx, parse_tool_id(tool_id), force=force)
    return ToolDeleted(message=DELETED_MESSAGE)
