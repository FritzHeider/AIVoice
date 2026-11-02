"""Bland-compatible ``POST /v1/calls`` and the stop endpoints (specs/05-calls-api.md §2, §5, §7.2, §7.3;
legacy bodies).

The route needs ``calls:write`` and one token of the ``calls_send`` bucket; an ``Idempotency-Key`` is
claimed with the scope check, so a replay returns the stored response without dispatching again. The BYO
Telnyx account is selected with the ``encrypted_key`` header. Everything else is
:func:`va_api.services.call_dispatch.dispatch_call`.
Call creation is recorded in ``calls`` and ``call_events``, not in the audit log.

``POST /v1/calls/{call_id}/stop`` and ``POST /v1/calls/active/stop`` need ``calls:write`` and the
``calls.operate`` permission (stop-all from a dashboard session also role ``owner``/``admin``); the work is
:mod:`va_api.services.call_stop`. The literal ``/active/stop`` is registered before ``/{call_id}/stop``.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import Depends, Header, Request

from va_api.audit import audit_exempt, audited
from va_api.auth.context import RequestContext
from va_api.auth.dependencies import rate_limit, require_scope
from va_api.auth.permissions import require_permission
from va_api.envelopes import legacy_router
from va_api.routers.calls_schemas import (
    SendCallResponse,
    StopActiveCallsRequest,
    StopActiveCallsResponse,
    StopCallResponse,
)
from va_api.services.call_dispatch import deps_of, dispatch_call
from va_api.services.call_stop import (
    check_stop_all_role,
    parse_call_id,
    stop_active_calls,
    stop_call,
    stop_deps_of,
)
from va_common.logging import get_logger
from va_domain.calls import SendCallRequest, unknown_fields

__all__ = ["calls_router", "calls_send", "calls_stop", "calls_stop_active"]

_log = get_logger(__name__)

calls_router = legacy_router(prefix="/v1/calls", tags=["Calls"])

WriteCalls = Annotated[RequestContext, Depends(require_scope("calls:write"))]
_DEPENDENCIES: Final = [Depends(rate_limit("calls_send"))]
_OPERATE: Final = [Depends(require_permission("calls.operate"))]
_STOP_EXTRA: Final = {"x-scope": "calls:write", "x-permission": "calls.operate", **audited("call.stop")}


@calls_router.post(
    "",
    operation_id="calls_send",
    dependencies=_DEPENDENCIES,
    openapi_extra={
        "x-scope": "calls:write",
        **audit_exempt("call creation is recorded in calls and call_events"),
    },
)
async def calls_send(
    body: SendCallRequest,
    request: Request,
    ctx: WriteCalls,
    encrypted_key: Annotated[
        str | None,
        Header(
            alias="encrypted_key",
            convert_underscores=False,
            description="Selects the org's BYO Telnyx account (specs/05 §2).",
        ),
    ] = None,
) -> SendCallResponse:
    """Place an outbound call now, or schedule it with ``start_time``."""
    # The body already validated as a JSON object, so the cached raw body is a dict.
    ignored = unknown_fields(await request.json())
    if ignored:
        _log.debug("calls.unknown_fields_ignored", fields=ignored)
    dispatched = await dispatch_call(deps_of(request), ctx, body, encrypted_key=encrypted_key)
    return SendCallResponse(
        message=dispatched.message, call_id=dispatched.call_id, scheduled_for=dispatched.scheduled_for
    )


@calls_router.post(
    "/active/stop", operation_id="calls_stop_active", dependencies=_OPERATE, openapi_extra=_STOP_EXTRA
)
async def calls_stop_active(
    request: Request, ctx: WriteCalls, body: StopActiveCallsRequest | None = None
) -> StopActiveCallsResponse:
    """Stop every active or scheduled call of the org, or only ``call_ids``."""
    check_stop_all_role(ctx)
    stopped = await stop_active_calls(stop_deps_of(request), ctx, None if body is None else body.call_ids)
    return StopActiveCallsResponse(
        message=stopped.message, stopped=len(stopped.call_ids), call_ids=stopped.call_ids
    )


@calls_router.post(
    "/{call_id}/stop", operation_id="calls_stop", dependencies=_OPERATE, openapi_extra=_STOP_EXTRA
)
async def calls_stop(call_id: str, request: Request, ctx: WriteCalls) -> StopCallResponse:
    """Cancel a call that is not answered yet, or hang up a call in progress."""
    outcome = await stop_call(stop_deps_of(request), ctx, parse_call_id(call_id))
    return StopCallResponse(message=outcome.message)
