"""``GET /v1/audit_logs``: the org's audit log, newest first (specs/04-auth-tenancy-rbac.md §13).

Needs the ``org:read`` scope (§2.3) and the ``audit.read`` permission (owners, admins, API keys with ``*``;
§3.2 "Audit logs" row). Filters are exact matches; ``since`` is inclusive and ``until`` exclusive, both with a
timezone. While a parent principal acts on a sub-account (``X-VA-Org-Id``) the sub-account's log is read.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Final

from fastapi import Depends, Query, Request

from va_api.auth.context import RequestContext
from va_api.auth.dependencies import require_scope
from va_api.auth.permissions import require_permission
from va_api.auth.session_auth import database_of
from va_api.envelopes import envelope_router
from va_api.responses import PageResponse, Pagination
from va_api.routers.audit_logs_schemas import AuditLogView, audit_log_view
from va_common.errors import AppError
from va_db.pagination import DEFAULT_PAGE_LIMIT
from va_db.repositories import AuditLogFilter, AuditLogRepo

__all__ = ["NAIVE_DATETIME_MESSAGE", "WINDOW_MESSAGE", "audit_logs_list", "audit_logs_router"]

NAIVE_DATETIME_MESSAGE: Final = "must include a timezone, e.g. 2026-10-29T14:03:11Z."
WINDOW_MESSAGE: Final = "since must not be later than until."
_PERMISSION: Final = "audit.read"

audit_logs_router = envelope_router(prefix="/v1/audit_logs", tags=["Audit Logs"])

ReadAudit = Annotated[RequestContext, Depends(require_scope("org:read"))]


def _aware(name: str, value: datetime | None) -> datetime | None:
    if value is not None and value.utcoffset() is None:
        raise AppError("INVALID_INPUT", f"{name} {NAIVE_DATETIME_MESSAGE}", field=name)
    return value


def _filters(
    *,
    action: Annotated[str | None, Query(description="Only this action, e.g. `api_key.create`.")] = None,
    actor_id: Annotated[uuid.UUID | None, Query(description="Only actions of this user or API key.")] = None,
    resource_type: Annotated[
        str | None, Query(description="Only this resource type, e.g. `api_key`.")
    ] = None,
    resource_id: Annotated[str | None, Query(description="Only this resource id.")] = None,
    since: Annotated[
        datetime | None, Query(description="Entries at or after this time (with timezone).")
    ] = None,
    until: Annotated[datetime | None, Query(description="Entries before this time (with timezone).")] = None,
) -> AuditLogFilter:
    filters = AuditLogFilter(
        action=action,
        actor_id=actor_id,
        resource_type=resource_type,
        resource_id=resource_id,
        since=_aware("since", since),
        until=_aware("until", until),
    )
    if filters.since is not None and filters.until is not None and filters.since > filters.until:
        raise AppError("INVALID_INPUT", WINDOW_MESSAGE, field="since")
    return filters


@audit_logs_router.get(
    "",
    operation_id="audit_logs_list",
    dependencies=[Depends(require_permission(_PERMISSION))],
    openapi_extra={"x-scope": "org:read", "x-permission": _PERMISSION},
)
async def audit_logs_list(
    request: Request,
    ctx: ReadAudit,
    filters: Annotated[AuditLogFilter, Depends(_filters)],
    cursor: Annotated[str | None, Query(description="`pagination.next_cursor` of the previous page.")] = None,
    limit: Annotated[int, Query(description="Page size, 1-100.")] = DEFAULT_PAGE_LIMIT,
) -> PageResponse[AuditLogView]:
    """The org's audit entries, newest first, filtered by action, actor, resource, and time window."""
    async with database_of(request).tenant_session(ctx.org_id) as session:
        page = await AuditLogRepo(session, ctx.org_id).page(filters, limit=limit, cursor=cursor)
        views = [audit_log_view(row) for row in page.items]
    return PageResponse(
        data=views, pagination=Pagination(next_cursor=page.next_cursor, has_more=page.has_more)
    )
