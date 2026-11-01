"""Bland-compatible ``POST /v1/calls`` (specs/05-calls-api.md §2, §5; legacy bodies).

The route needs ``calls:write`` and one token of the ``calls_send`` bucket; an ``Idempotency-Key`` is
claimed with the scope check, so a replay returns the stored response without dispatching again. The BYO
Telnyx account is selected with the ``encrypted_key`` header. Everything else is
:func:`va_api.services.call_dispatch.dispatch_call`.
Call creation is recorded in ``calls`` and ``call_events``, not in the audit log.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import Depends, Header, Request

from va_api.audit import audit_exempt
from va_api.auth.context import RequestContext
from va_api.auth.dependencies import rate_limit, require_scope
from va_api.envelopes import legacy_router
from va_api.routers.calls_schemas import SendCallResponse
from va_api.services.call_dispatch import deps_of, dispatch_call
from va_common.logging import get_logger
from va_domain.calls import SendCallRequest, unknown_fields

__all__ = ["calls_router", "calls_send"]

_log = get_logger(__name__)

calls_router = legacy_router(prefix="/v1/calls", tags=["Calls"])

WriteCalls = Annotated[RequestContext, Depends(require_scope("calls:write"))]
_DEPENDENCIES: Final = [Depends(rate_limit("calls_send"))]


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
