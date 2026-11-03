"""Org webhook endpoints and delivery history under ``/v1/webhooks`` (specs/19-events-and-webhooks.md §10).

Rules: specs/19 §10 Decision P06-T04b. Reads need ``webhooks:read`` and changes ``webhooks:write``
(specs/04 §2.3, §3.2 "Webhook endpoints / deliveries / replay"); turning ``phi_allowed`` on also needs
``org.compliance``.
Endpoint URLs are vetted with the Send Call webhook rules (``WEBHOOK_URL_REJECTED`` on field ``url``). An org
has at most 20 endpoints. Deleting an endpoint cancels its queued deliveries in the same transaction. The test
send makes one signed, unrecorded request. A replay re-queues a finished delivery as a new attempt series of
the same row (same event id). The routes attach to :data:`va_api.routers.webhooks.webhooks_router`
(``va_api.main`` imports this module for that side effect).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final

from fastapi import Depends, Query, Request

from va_api.audit import audit, audit_exempt, audited, changed
from va_api.auth.context import RequestContext
from va_api.auth.dependencies import require_scope
from va_api.auth.permissions import holds_permission
from va_api.auth.session_auth import clock_of, database_of, envelope_of, org_settings_of, settings_of
from va_api.responses import DataResponse, PageResponse, Pagination
from va_api.routers.webhook_endpoints_schemas import (
    WebhookDeliveryView,
    WebhookEndpointCreate,
    WebhookEndpointDeleted,
    WebhookEndpointPatch,
    WebhookEndpointView,
    WebhookTestRequest,
    WebhookTestResult,
    audited_endpoint,
    delivery_view,
    endpoint_view,
)
from va_api.routers.webhooks import webhooks_router
from va_api.services.webhook_policy import check_webhook_url
from va_api.services.webhook_test_send import SyntheticDeliveryDeps, send_test_event
from va_common.errors import AppError
from va_db.models.billing import WebhookDeliveryStatus
from va_db.pagination import DEFAULT_PAGE_LIMIT
from va_db.repositories import WebhookDeliveryFilter, WebhookDeliveryRepo, WebhookEndpointRepo
from va_domain.org import OrgSettings
from va_events import EventType

__all__ = [
    "MAX_ENDPOINTS",
    "TOO_MANY_ENDPOINTS_MESSAGE",
    "webhooks_deliveries_list",
    "webhooks_deliveries_replay",
    "webhooks_endpoints_create",
    "webhooks_endpoints_delete",
    "webhooks_endpoints_list",
    "webhooks_endpoints_test",
    "webhooks_endpoints_update",
]

MAX_ENDPOINTS: Final = 20
TOO_MANY_ENDPOINTS_MESSAGE: Final = f"An org can have at most {MAX_ENDPOINTS} webhook endpoints."
_COMPLIANCE: Final = "org.compliance"
_READ: Final = {"x-scope": "webhooks:read"}
_WRITE: Final = {"x-scope": "webhooks:write"}
_RESOURCE: Final = "webhook_endpoint"
_CREATE: Final = "webhook_endpoint.create"
_UPDATE: Final = "webhook_endpoint.update"
_DELETE: Final = "webhook_endpoint.delete"
_REPLAY: Final = "webhook_delivery.replay"

ReadWebhooks = Annotated[RequestContext, Depends(require_scope("webhooks:read"))]
WriteWebhooks = Annotated[RequestContext, Depends(require_scope("webhooks:write"))]


def _check_phi(ctx: RequestContext, sent: dict[str, Any]) -> None:
    if sent.get("phi_allowed") is True and not holds_permission(ctx, _COMPLIANCE):
        raise AppError("FORBIDDEN", f"Missing required permission '{_COMPLIANCE}'.")


async def _org_settings(request: Request, ctx: RequestContext) -> OrgSettings:
    found = await org_settings_of(request).get(ctx.org_id)
    return found if found is not None else OrgSettings()


async def _vet_url(request: Request, ctx: RequestContext, url: str) -> None:
    await check_webhook_url(
        url,
        org=await _org_settings(request, ctx),
        settings=settings_of(request),
        field="url",
        resolver=request.app.state.container.host_resolver,
    )


@webhooks_router.get("/endpoints", operation_id="webhooks_endpoints_list", openapi_extra=_READ)
async def webhooks_endpoints_list(
    request: Request, ctx: ReadWebhooks
) -> DataResponse[list[WebhookEndpointView]]:
    """Every webhook endpoint of the org, oldest first."""
    async with database_of(request).tenant_session(ctx.org_id) as session:
        rows = await WebhookEndpointRepo(session, ctx.org_id).all()
        views = [endpoint_view(row) for row in rows]
    return DataResponse(data=views)


@webhooks_router.post(
    "/endpoints",
    operation_id="webhooks_endpoints_create",
    status_code=201,
    openapi_extra={**_WRITE, **audited(_CREATE)},
)
async def webhooks_endpoints_create(
    request: Request, ctx: WriteWebhooks, body: WebhookEndpointCreate
) -> DataResponse[WebhookEndpointView]:
    """Add an endpoint that receives the org's events (envelope format, specs/19 §7)."""
    _check_phi(ctx, body.model_dump())
    await _vet_url(request, ctx, body.url)
    async with database_of(request).tenant_session(ctx.org_id) as session:
        repo = WebhookEndpointRepo(session, ctx.org_id, clock=clock_of(request))
        if await repo.count() >= MAX_ENDPOINTS:
            raise AppError("INVALID_INPUT", TOO_MANY_ENDPOINTS_MESSAGE)
        row = await repo.create(body.model_dump())
        await audit(session, ctx, _CREATE, _RESOURCE, row.id, after=audited_endpoint(row))
        view = endpoint_view(row)
    return DataResponse(data=view)


@webhooks_router.patch(
    "/endpoints/{endpoint_id}",
    operation_id="webhooks_endpoints_update",
    openapi_extra={**_WRITE, **audited(_UPDATE)},
)
async def webhooks_endpoints_update(
    request: Request, ctx: WriteWebhooks, endpoint_id: uuid.UUID, body: WebhookEndpointPatch
) -> DataResponse[WebhookEndpointView]:
    """Change any subset of an endpoint's fields; queued deliveries keep the URL they were created for."""
    sent = body.model_dump(exclude_unset=True)
    _check_phi(ctx, sent)
    if "url" in sent:
        await _vet_url(request, ctx, sent["url"])
    async with database_of(request).tenant_session(ctx.org_id) as session:
        repo = WebhookEndpointRepo(session, ctx.org_id, clock=clock_of(request))
        before = audited_endpoint(await repo.get(endpoint_id))
        row = await repo.update(endpoint_id, sent) if sent else await repo.get(endpoint_id)
        old, new = changed(before, audited_endpoint(row))
        if new:
            await audit(session, ctx, _UPDATE, _RESOURCE, row.id, before=old, after=new)
        view = endpoint_view(row)
    return DataResponse(data=view)


@webhooks_router.delete(
    "/endpoints/{endpoint_id}",
    operation_id="webhooks_endpoints_delete",
    openapi_extra={**_WRITE, **audited(_DELETE)},
)
async def webhooks_endpoints_delete(
    request: Request, ctx: WriteWebhooks, endpoint_id: uuid.UUID
) -> DataResponse[WebhookEndpointDeleted]:
    """Delete an endpoint; its pending deliveries are cancelled (`dead`, `endpoint_deleted`)."""
    async with database_of(request).tenant_session(ctx.org_id) as session:
        repo = WebhookEndpointRepo(session, ctx.org_id)
        before = audited_endpoint(await repo.get(endpoint_id))
        cancelled = await repo.delete(endpoint_id)
        await audit(
            session,
            ctx,
            _DELETE,
            _RESOURCE,
            endpoint_id,
            before=before,
            after={"cancelled_deliveries": cancelled},
        )
    return DataResponse(data=WebhookEndpointDeleted(deleted=True))


@webhooks_router.post(
    "/endpoints/{endpoint_id}/test",
    operation_id="webhooks_endpoints_test",
    openapi_extra={**_WRITE, **audit_exempt("synthetic test delivery; stores nothing")},
)
async def webhooks_endpoints_test(
    request: Request, ctx: WriteWebhooks, endpoint_id: uuid.UUID, body: WebhookTestRequest | None = None
) -> DataResponse[WebhookTestResult]:
    """Send one synthetic signed event to the endpoint (also when disabled) and report what happened."""
    chosen = body if body is not None else WebhookTestRequest()
    container = request.app.state.container
    deps = SyntheticDeliveryDeps(
        database=database_of(request),
        envelope=envelope_of(request),
        deliverer=container.webhook_deliverer,
        settings=settings_of(request),
        resolver=container.host_resolver,
        clock=clock_of(request),
    )
    sent = await send_test_event(
        deps, org_id=ctx.org_id, endpoint_id=endpoint_id, event_type=EventType(chosen.event_type)
    )
    result = sent.result
    return DataResponse(
        data=WebhookTestResult(
            event_id=sent.event_id,
            event_type=sent.event_type,
            succeeded=result.succeeded,
            status_code=result.status_code,
            error=result.error,
            response_ms=result.response_ms,
        )
    )


def _delivery_filters(
    *,
    status: Annotated[
        WebhookDeliveryStatus | None, Query(description="Only this status (`dead` = the dead-letter view).")
    ] = None,
    event_type: Annotated[
        str | None, Query(description="Only this event type, e.g. `call.completed`.")
    ] = None,
    call_id: Annotated[uuid.UUID | None, Query(description="Only deliveries about this call.")] = None,
    endpoint_id: Annotated[uuid.UUID | None, Query(description="Only deliveries to this endpoint.")] = None,
) -> WebhookDeliveryFilter:
    return WebhookDeliveryFilter(
        status=status, event_type=event_type, call_id=call_id, endpoint_id=endpoint_id
    )


@webhooks_router.get("/deliveries", operation_id="webhooks_deliveries_list", openapi_extra=_READ)
async def webhooks_deliveries_list(
    request: Request,
    ctx: ReadWebhooks,
    filters: Annotated[WebhookDeliveryFilter, Depends(_delivery_filters)],
    cursor: Annotated[str | None, Query(description="`pagination.next_cursor` of the previous page.")] = None,
    limit: Annotated[int, Query(description="Page size, 1-100.")] = DEFAULT_PAGE_LIMIT,
) -> PageResponse[WebhookDeliveryView]:
    """The org's webhook deliveries (per-call webhooks and endpoints), newest first, without payloads."""
    async with database_of(request).tenant_session(ctx.org_id) as session:
        page = await WebhookDeliveryRepo(session, ctx.org_id).page(filters, limit=limit, cursor=cursor)
        views = [delivery_view(row) for row in page.items]
    return PageResponse(
        data=views, pagination=Pagination(next_cursor=page.next_cursor, has_more=page.has_more)
    )


@webhooks_router.post(
    "/deliveries/{delivery_id}/replay",
    operation_id="webhooks_deliveries_replay",
    openapi_extra={**_WRITE, **audited(_REPLAY)},
)
async def webhooks_deliveries_replay(
    request: Request, ctx: WriteWebhooks, delivery_id: uuid.UUID
) -> DataResponse[WebhookDeliveryView]:
    """Queue a finished delivery again: a new attempt series with the same event id, URL, and payload."""
    async with database_of(request).tenant_session(ctx.org_id) as session:
        repo = WebhookDeliveryRepo(session, ctx.org_id)
        current = await repo.get(delivery_id)
        before = {"status": current.status, "attempts": current.attempts, "last_error": current.last_error}
        row = await repo.replay(delivery_id, now=clock_of(request).now())
        await audit(
            session, ctx, _REPLAY, "webhook_delivery", row.id, before=before, after={"status": "pending"}
        )
        view = delivery_view(row)
    return DataResponse(data=view)
