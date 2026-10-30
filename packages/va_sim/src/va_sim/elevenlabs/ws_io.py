"""WebSocket helpers shared by the FakeElevenLabs TTS and STT sockets: JSON receive, quiet close, codes."""

from __future__ import annotations

from typing import Final, Literal

import orjson
from starlette.websockets import WebSocket, WebSocketDisconnect

__all__ = [
    "INTERNAL_ERROR",
    "NORMAL_CLOSURE",
    "POLICY_VIOLATION",
    "SERVICE_RESTART",
    "Message",
    "Received",
    "close_quietly",
    "close_reason",
    "receive_json",
    "send_json",
]

NORMAL_CLOSURE: Final = 1000
POLICY_VIOLATION: Final = 1008
INTERNAL_ERROR: Final = 1011
SERVICE_RESTART: Final = 1012
_MAX_REASON_BYTES: Final = 123  # RFC 6455 §5.5: a close frame's payload is at most 125 bytes incl. the code

type Message = dict[str, object]
type Received = Message | Literal["disconnect", "invalid"]


async def receive_json(ws: WebSocket) -> Received:
    """The next message as a JSON object; ``"invalid"`` for non-JSON or non-object messages."""
    message = await ws.receive()
    if message["type"] == "websocket.disconnect":
        return "disconnect"
    raw = message.get("text")
    if raw is None:
        raw = message.get("bytes")
    if not isinstance(raw, str | bytes):
        return "invalid"
    try:
        parsed = orjson.loads(raw)
    except orjson.JSONDecodeError:
        return "invalid"
    return parsed if isinstance(parsed, dict) else "invalid"


async def send_json(ws: WebSocket, payload: Message) -> bool:
    """Send ``payload`` as a JSON text frame; ``False`` when the peer is gone."""
    try:
        await ws.send_text(orjson.dumps(payload).decode())
    except (WebSocketDisconnect, OSError, RuntimeError):
        return False
    return True


def close_reason(text: str) -> str:
    """``text`` cut to the 123 UTF-8 bytes a close frame can carry (never splitting a character)."""
    return text.encode()[:_MAX_REASON_BYTES].decode(errors="ignore")


async def close_quietly(ws: WebSocket, code: int, reason: str) -> None:
    """Close with ``code`` and ``reason`` (truncated to fit); a peer that already left is not an error."""
    try:
        await ws.close(code, close_reason(reason))
    except (RuntimeError, OSError, WebSocketDisconnect):
        return
