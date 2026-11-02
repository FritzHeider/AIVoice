"""Bland-compatible ``POST /v1/calls`` and the stop endpoints (specs/05-calls-api.md §2, §5, §7.2, §7.3;
legacy bodies).

The route needs ``calls:write`` and one token of the ``calls_send`` bucket; an ``Idempotency-Key`` is
claimed with the scope check, so a replay returns the stored response without dispatching again. The BYO
Telnyx account is selected with the ``encrypted_key`` header. Everything else is
:func:`va_api.services.call_dispatch.dispatch_call`.
Call creation is recorded in ``calls`` and ``call_events``, not in the audit log.

``GET /v1/calls`` (page of Call Object list items, every specs/05 §6 filter) and ``GET /v1/calls/{call_id}``
(the bare Call Object, specs/05 §7.1) need ``calls:read``; the work is :mod:`va_api.services.call_read`.

``GET /v1/calls/active`` (active calls with their pathway node), ``GET /v1/calls/{call_id}/recording``
(302 to a 15-minute link, or JSON with ``format=json``; ``channel`` mixed/caller/agent),
``GET /v1/calls/{call_id}/transcript`` (JSON, text, SRT, or WebVTT), and ``GET /v1/calls/{call_id}/events``
(``after_seq`` paging) need ``calls:read``; the work is :mod:`va_api.services.call_artifacts`. The literal
``/active`` is registered before ``/{call_id}``.

``POST /v1/calls/{call_id}/stop`` and ``POST /v1/calls/active/stop`` need ``calls:write`` and the
``calls.operate`` permission (stop-all from a dashboard session also role ``owner``/``admin``); the work is
:mod:`va_api.services.call_stop`. The literal ``/active/stop`` is registered before ``/{call_id}/stop``.
"""

from __future__ import annotations

from typing import Annotated, Any, Final

from fastapi import Depends, Header, Query, Request, Response
from fastapi.responses import PlainTextResponse, RedirectResponse

from va_api.audit import audit_exempt, audited
from va_api.auth.context import RequestContext
from va_api.auth.dependencies import rate_limit, require_scope
from va_api.auth.permissions import require_permission
from va_api.envelopes import legacy_router
from va_api.routers.calls_detail_schemas import (
    ActiveCallsResponse,
    CallEventsQuery,
    CallEventsResponse,
    RecordingQuery,
    RecordingResponse,
    TranscriptQuery,
    TranscriptResponse,
)
from va_api.routers.calls_read_schemas import CallListQuery, CallListResponse
from va_api.routers.calls_schemas import (
    SendCallResponse,
    StopActiveCallsRequest,
    StopActiveCallsResponse,
    StopCallResponse,
)
from va_api.services.call_artifacts import active_calls, call_events, recording_link, transcript
from va_api.services.call_dispatch import deps_of, dispatch_call
from va_api.services.call_read import get_call, list_calls, read_deps_of
from va_api.services.call_stop import (
    check_stop_all_role,
    parse_call_id,
    stop_active_calls,
    stop_call,
    stop_deps_of,
)
from va_common.logging import get_logger
from va_domain.call_object import CallObject, concatenated_transcript, to_srt, to_vtt
from va_domain.calls import SendCallRequest, unknown_fields

__all__ = [
    "calls_active",
    "calls_events",
    "calls_get",
    "calls_list",
    "calls_recording",
    "calls_router",
    "calls_send",
    "calls_stop",
    "calls_stop_active",
    "calls_transcript",
]

_log = get_logger(__name__)

calls_router = legacy_router(prefix="/v1/calls", tags=["Calls"])

WriteCalls = Annotated[RequestContext, Depends(require_scope("calls:write"))]
ReadCalls = Annotated[RequestContext, Depends(require_scope("calls:read"))]
_DEPENDENCIES: Final = [Depends(rate_limit("calls_send"))]
_OPERATE: Final = [Depends(require_permission("calls.operate"))]
_STOP_EXTRA: Final = {"x-scope": "calls:write", "x-permission": "calls.operate", **audited("call.stop")}
_READ_EXTRA: Final = {"x-scope": "calls:read"}
_REDIRECT: Final = 302
_NO_STORE: Final = {"Cache-Control": "no-store"}
_RECORDING_DOC: Final[dict[int | str, dict[str, Any]]] = {
    _REDIRECT: {
        "description": "Redirect to a 15-minute presigned recording URL (without `format=json`).",
        "headers": {"Location": {"description": "Presigned URL.", "schema": {"type": "string"}}},
    }
}
_TRANSCRIPT_DOC: Final[dict[int | str, dict[str, Any]]] = {
    200: {
        "description": "The transcript; `text`, `srt`, and `vtt` answer plain text in that format.",
        "content": {"text/plain": {}, "application/x-subrip": {}, "text/vtt": {}},
    }
}
_SRT_MEDIA_TYPE: Final = "application/x-subrip; charset=utf-8"
_VTT_MEDIA_TYPE: Final = "text/vtt; charset=utf-8"


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


@calls_router.get("/active", operation_id="calls_active", openapi_extra=_READ_EXTRA)
async def calls_active(request: Request, ctx: ReadCalls) -> ActiveCallsResponse:
    """The org's dialing, ringing, in-progress, and transferring calls, oldest first."""
    calls = await active_calls(read_deps_of(request), ctx)
    return ActiveCallsResponse(count=len(calls), calls=calls)


@calls_router.get("", operation_id="calls_list", openapi_extra=_READ_EXTRA)
async def calls_list(
    request: Request, ctx: ReadCalls, query: Annotated[CallListQuery, Query()]
) -> CallListResponse:
    """List the org's calls, newest first, with Bland's filters and cursor paging."""
    page = await list_calls(read_deps_of(request), ctx, query)
    return CallListResponse(
        total_count=page.total_count, count=len(page.calls), next_cursor=page.next_cursor, calls=page.calls
    )


@calls_router.get("/{call_id}", operation_id="calls_get", openapi_extra=_READ_EXTRA)
async def calls_get(call_id: str, request: Request, ctx: ReadCalls) -> CallObject:
    """The Call Object of one call, with a fresh 7-day recording URL."""
    return await get_call(read_deps_of(request), ctx, parse_call_id(call_id))


@calls_router.get(
    "/{call_id}/recording",
    operation_id="calls_recording",
    response_model=RecordingResponse,
    responses=_RECORDING_DOC,
    openapi_extra=_READ_EXTRA,
)
async def calls_recording(
    call_id: str,
    request: Request,
    response: Response,
    ctx: ReadCalls,
    query: Annotated[RecordingQuery, Query()],
) -> RecordingResponse | Response:
    """Redirect to the call's recording (15-minute link), or answer the link as JSON with `format=json`."""
    link = await recording_link(read_deps_of(request), ctx, parse_call_id(call_id), query.channel)
    if query.format is None:
        return RedirectResponse(link.url, status_code=_REDIRECT, headers=_NO_STORE)
    response.headers.update(_NO_STORE)
    return RecordingResponse(
        url=link.url, expires_at=link.expires_at, duration_ms=link.duration_ms, channels=link.channels
    )


@calls_router.get(
    "/{call_id}/transcript",
    operation_id="calls_transcript",
    response_model=TranscriptResponse,
    responses=_TRANSCRIPT_DOC,
    openapi_extra=_READ_EXTRA,
)
async def calls_transcript(
    call_id: str, request: Request, ctx: ReadCalls, query: Annotated[TranscriptQuery, Query()]
) -> TranscriptResponse | Response:
    """The call's transcript as JSON, plain text, SubRip, or WebVTT."""
    parsed = parse_call_id(call_id)
    turns = await transcript(read_deps_of(request), ctx, parsed)
    if query.format == "text":
        return PlainTextResponse(concatenated_transcript(turns))
    if query.format == "srt":
        return Response(to_srt(turns), media_type=_SRT_MEDIA_TYPE)
    if query.format == "vtt":
        return Response(to_vtt(turns), media_type=_VTT_MEDIA_TYPE)
    return TranscriptResponse(
        call_id=parsed, transcripts=turns, concatenated_transcript=concatenated_transcript(turns)
    )


@calls_router.get("/{call_id}/events", operation_id="calls_events", openapi_extra=_READ_EXTRA)
async def calls_events(
    call_id: str, request: Request, ctx: ReadCalls, query: Annotated[CallEventsQuery, Query()]
) -> CallEventsResponse:
    """A page of the call's event log ordered by `seq`; pass `next_after_seq` as `after_seq` for the next."""
    page = await call_events(read_deps_of(request), ctx, parse_call_id(call_id), query)
    return CallEventsResponse(events=page.events, next_after_seq=page.next_after_seq)
