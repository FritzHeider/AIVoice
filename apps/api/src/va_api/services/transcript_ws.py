"""The real-time transcript of a call over a WebSocket (specs/19-events-and-webhooks.md §9; Decision
P06-T06b).

:func:`issue_transcript_token` mints a 60 s token for one call of the caller's org
(:mod:`va_api.services.transcript_tokens`). ``WS /v1/calls/{call_id}/transcript/ws?token=`` authenticates
with that token only: :func:`open_transcript` resolves it (any refusal is ``AUTH_FAILURE`` with one
message; the reason is logged as ``calls.transcript_ws_refused``), checks the call (``CALL_NOT_FOUND``), and
subscribes the live channel (``SERVICE_UNAVAILABLE``) before the handshake is accepted.
:func:`serve_transcript` then sends one JSON text message ``{"id": <seq>, "event": <type>, "data":
<bland-live body>}`` per ``call.turn`` / ``call.post_transfer_transcript`` event: every stored one, then the
live ones, over the shared tail of :mod:`va_api.services.call_tail`. The server closes with 1000
``completed`` 30 s after ``call.completed`` (at once for an older call), 1000 ``max_duration`` after 4 h,
and 1011 ``overflow`` / ``lost`` / ``closed`` when the live subscription ends; client messages are ignored.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass
from typing import Final

from redis.asyncio import Redis
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocket, WebSocketDisconnect

from va_api.auth.context import RequestContext
from va_api.services.call_tail import CallTail, StreamDeps, call_metadata, subscribe_tail
from va_api.services.transcript_tokens import MintedToken, TranscriptTokenError, TranscriptTokens
from va_common.errors import AppError
from va_common.json import dumps
from va_common.logging import get_logger
from va_events import EventType

__all__ = [
    "INVALID_TOKEN_MESSAGE",
    "NORMAL_CLOSE",
    "TRANSCRIPT_TYPES",
    "UNAVAILABLE_CLOSE",
    "TranscriptDeps",
    "issue_transcript_token",
    "open_transcript",
    "serve_transcript",
    "transcript_deps_of",
]

TRANSCRIPT_TYPES: Final = frozenset(
    {EventType.CALL_TURN.value, EventType.CALL_POST_TRANSFER_TRANSCRIPT.value}
)
INVALID_TOKEN_MESSAGE: Final = "The transcript token is invalid or expired."  # noqa: S105 - a message
NORMAL_CLOSE: Final = 1000
UNAVAILABLE_CLOSE: Final = 1011
_NORMAL_REASONS: Final = frozenset({"completed", "max_duration"})

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TranscriptDeps:
    """What the token route and the transcript socket need from the container."""

    stream: StreamDeps
    tokens: TranscriptTokens


def transcript_deps_of(connection: HTTPConnection) -> TranscriptDeps:
    """The transcript dependencies of the running app (request or WebSocket)."""
    container = connection.app.state.container
    redis: Redis = container.redis
    stream = StreamDeps(database=container.database, live_hub=container.live_hub, clock=container.clock)
    return TranscriptDeps(stream=stream, tokens=TranscriptTokens(redis, container.clock))


async def issue_transcript_token(
    deps: TranscriptDeps, ctx: RequestContext, call_id: uuid.UUID
) -> MintedToken:
    """A token for the transcript socket of ``call_id``; ``CALL_NOT_FOUND`` unless the call is the org's."""
    await call_metadata(deps.stream, ctx.org_id, call_id)
    return await deps.tokens.mint(ctx.org_id, call_id)


async def open_transcript(deps: TranscriptDeps, call_id: str, token: str | None) -> CallTail:
    """Resolve the token, check the call, and subscribe its live channel; the tail is not started."""
    try:
        record = await deps.tokens.resolve(token, call_id)
    except TranscriptTokenError as refused:
        _log.info("calls.transcript_ws_refused", call_id=call_id, reason=refused.reason)
        raise AppError("AUTH_FAILURE", INVALID_TOKEN_MESSAGE) from refused
    metadata = await call_metadata(deps.stream, record.org_id, record.call_id)
    return await subscribe_tail(
        deps.stream,
        org_id=record.org_id,
        call_id=record.call_id,
        metadata=metadata,
        after=0,
        types=TRANSCRIPT_TYPES,
        heartbeat_s=None,
    )


async def serve_transcript(websocket: WebSocket, tail: CallTail, call_id: str) -> None:
    """Accept the socket and send the tail's items until it ends or the client goes away."""
    sent = 0

    async def send_items() -> bool:
        nonlocal sent
        try:
            async with contextlib.aclosing(tail.items()) as items:
                async for item in items:
                    if item is None:
                        continue
                    message = {"id": item.seq, "event": item.event_type, "data": dict(item.body)}
                    await websocket.send_text(dumps(message).decode())
                    sent += 1
        except WebSocketDisconnect:
            return False
        return True

    try:
        await websocket.accept()
        sender = asyncio.ensure_future(send_items())
        receiver = asyncio.ensure_future(_until_disconnect(websocket))
        try:
            await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            receiver.cancel()
            if not sender.done():
                sender.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
            ended = await _ended_by_server(sender)
        if ended:
            code = NORMAL_CLOSE if tail.reason in _NORMAL_REASONS else UNAVAILABLE_CLOSE
            with contextlib.suppress(WebSocketDisconnect, RuntimeError, OSError):
                await websocket.close(code=code, reason=tail.reason)
    finally:
        await tail.aclose()
        _log.info("calls.transcript_ws_closed", call_id=call_id, reason=tail.reason, messages=sent)


async def _until_disconnect(websocket: WebSocket) -> None:
    """Read and ignore client messages until the client disconnects."""
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return


async def _ended_by_server(sender: asyncio.Future[bool]) -> bool:
    """Whether the tail itself ended (``False`` when the client went away first); re-raises a failure."""
    try:
        return await sender
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise
        return False
