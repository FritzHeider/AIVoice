"""The org event stream under ``/v1/events`` (specs/19-events-and-webhooks.md §9; Decision P06-T06c).

``GET /v1/events/stream?types=`` streams the org's new events as Server-Sent Events, rendered exactly as an
org webhook endpoint receives them. It needs ``webhooks:read``: API keys with that scope (``va webhooks
listen``) and dashboard sessions of every role but ``billing`` (specs/04 §3.2 row "Webhook endpoints /
deliveries / replay"). The scope is ``ctx.org_id``: a parent acting on a sub-account sees that sub-account,
never descendants. The operation is tagged ``Webhooks`` (specs/26 §1 tag list). The work is
:mod:`va_api.services.org_stream`.
"""

from __future__ import annotations

from typing import Annotated, Any, Final

from fastapi import Depends, Query, Request

from va_api.auth.context import RequestContext
from va_api.auth.dependencies import require_scope
from va_api.envelopes import envelope_router
from va_api.services.org_stream import open_org_stream, org_stream_deps_of
from va_api.sse import EventStreamResponse

__all__ = ["events_router", "events_stream"]

events_router = envelope_router(prefix="/v1/events", tags=["Webhooks"])

ReadWebhooks = Annotated[RequestContext, Depends(require_scope("webhooks:read"))]

_STREAM_DOC: Final[dict[int | str, dict[str, Any]]] = {
    200: {
        "description": (
            "Server-Sent Events: `id: <event id>`, `event: <type>`, `data: <webhook envelope JSON>` "
            "(the body an org webhook endpoint receives); `: ping` comments every 15 s. New events only: "
            "no replay, `Last-Event-ID` is ignored."
        ),
        "content": {"text/event-stream": {"schema": {"type": "string"}}},
    }
}


@events_router.get(
    "/stream",
    operation_id="events_stream",
    openapi_extra={"x-scope": "webhooks:read"},
    response_class=EventStreamResponse,
    responses=_STREAM_DOC,
)
async def events_stream(
    request: Request,
    ctx: ReadWebhooks,
    types: Annotated[
        str | None,
        Query(
            description="Comma-separated event type globs (default `*`), e.g. `call.*,sms.received`.",
            max_length=1_400,
        ),
    ] = None,
) -> EventStreamResponse:
    """The org's new events as Server-Sent Events, filtered by `types`; after a reconnect, re-read current
    state (for example `GET /v1/calls/active`)."""
    stream = await open_org_stream(org_stream_deps_of(request), ctx, types)
    return EventStreamResponse(stream.frames(), stream.aclose)
