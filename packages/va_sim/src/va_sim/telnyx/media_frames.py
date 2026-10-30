"""FakeTelnyx media WebSocket frames (specs/06-telephony-telnyx.md §8; specs/30-testing-and-evals.md §3.2,
Decision 2026-10-29 P03-T03b rules 4 and 6).

Builders for every frame the simulator sends (``connected``, ``start``, ``media``, ``dtmf``, ``mark``,
``error``, ``stop``) and the parser of agent frames (``media``, ``mark``, ``clear``). The simulator builds
these from the spec on its own; it shares no code with ``va_telnyx.media``. ``sequence_number``, ``chunk``,
and ``timestamp`` are decimal strings as Telnyx sends them.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Any, Final

import orjson

from va_sim.telnyx.media_format import FRAME_MS, MediaFormat

__all__ = [
    "INVALID_MEDIA",
    "MALFORMED_FRAME",
    "MAX_AGENT_MESSAGE_MS",
    "MEDIA_VERSION",
    "AgentClear",
    "AgentFrame",
    "AgentMark",
    "AgentMedia",
    "MediaFault",
    "StreamIdentity",
    "connected_frame",
    "dtmf_frame",
    "error_frame",
    "mark_frame",
    "media_frame",
    "parse_agent_frame",
    "start_frame",
    "stop_frame",
]

MEDIA_VERSION: Final = "1.0.0"
MALFORMED_FRAME: Final = 100003
INVALID_MEDIA: Final = 100004
MAX_AGENT_MESSAGE_MS: Final = 30_000
_TITLES: Final = {MALFORMED_FRAME: "malformed_frame", INVALID_MEDIA: "invalid_media"}

type Frame = dict[str, Any]


@dataclass(frozen=True, slots=True)
class StreamIdentity:
    """What the ``start`` and ``stop`` frames say about the stream and its call."""

    stream_id: str
    user_id: str
    call_control_id: str
    call_session_id: str
    from_: str
    to: str
    client_state: str | None


def connected_frame() -> Frame:
    return {"event": "connected", "version": MEDIA_VERSION}


def start_frame(identity: StreamIdentity, fmt: MediaFormat) -> Frame:
    return {
        "event": "start",
        "sequence_number": "1",
        "start": {
            "user_id": identity.user_id,
            "call_control_id": identity.call_control_id,
            "call_session_id": identity.call_session_id,
            "from": identity.from_,
            "to": identity.to,
            "tags": [],
            "client_state": identity.client_state,
            "media_format": fmt.media_format(),
        },
        "stream_id": identity.stream_id,
    }


def media_frame(stream_id: str, sequence_number: int, chunk: int, payload: bytes) -> Frame:
    """Caller audio chunk ``chunk`` (from 1); its timestamp is the ms since the stream started."""
    return {
        "event": "media",
        "sequence_number": str(sequence_number),
        "media": {
            "track": "inbound",
            "chunk": str(chunk),
            "timestamp": str(FRAME_MS * (chunk - 1)),
            "payload": base64.b64encode(payload).decode("ascii"),
        },
        "stream_id": stream_id,
    }


def dtmf_frame(stream_id: str, sequence_number: int, digit: str, occurred_at: str) -> Frame:
    return {
        "event": "dtmf",
        "stream_id": stream_id,
        "occurred_at": occurred_at,
        "sequence_number": str(sequence_number),
        "dtmf": {"digit": digit},
    }


def mark_frame(stream_id: str, sequence_number: int, name: str) -> Frame:
    return {
        "event": "mark",
        "stream_id": stream_id,
        "sequence_number": str(sequence_number),
        "mark": {"name": name},
    }


def error_frame(stream_id: str, code: int, detail: str) -> Frame:
    """A Telnyx ``error`` frame (no sequence number); the connection stays open."""
    return {
        "event": "error",
        "payload": {"code": code, "title": _TITLES[code], "detail": detail},
        "stream_id": stream_id,
    }


def stop_frame(identity: StreamIdentity, sequence_number: int) -> Frame:
    return {
        "event": "stop",
        "sequence_number": str(sequence_number),
        "stop": {"user_id": identity.user_id, "call_control_id": identity.call_control_id},
        "stream_id": identity.stream_id,
    }


@dataclass(frozen=True, slots=True)
class AgentMedia:
    """Agent audio in the agent format (wire bytes, base64-decoded)."""

    audio: bytes


@dataclass(frozen=True, slots=True)
class AgentMark:
    name: str


@dataclass(frozen=True, slots=True)
class AgentClear:
    """Drop queued agent audio."""


@dataclass(frozen=True, slots=True)
class MediaFault:
    """A frame the simulator answers with a Telnyx ``error`` frame."""

    code: int
    detail: str


type AgentFrame = AgentMedia | AgentMark | AgentClear


def _malformed(detail: str) -> MediaFault:
    return MediaFault(MALFORMED_FRAME, detail)


def _invalid_media(detail: str) -> MediaFault:
    return MediaFault(INVALID_MEDIA, detail)


def _parse_media(frame: Frame, fmt: MediaFormat) -> AgentMedia | MediaFault:
    media = frame.get("media")
    payload = media.get("payload") if isinstance(media, dict) else None
    if not isinstance(payload, str):
        return _invalid_media("media.payload must be a base64 string")
    try:
        audio = base64.b64decode(payload, validate=True)
    except binascii.Error:
        return _invalid_media("media.payload is not valid base64")
    if not audio:
        return _invalid_media("media.payload is empty")
    if len(audio) % fmt.sample_bytes:
        return _invalid_media(f"{fmt.encoding} audio needs whole {fmt.sample_bytes}-byte samples")
    if len(audio) > MAX_AGENT_MESSAGE_MS * fmt.bytes_per_ms:
        return _invalid_media(f"a media message carries at most {MAX_AGENT_MESSAGE_MS} ms of audio")
    return AgentMedia(audio)


def _parse_mark(frame: Frame) -> AgentMark | MediaFault:
    mark = frame.get("mark")
    name = mark.get("name") if isinstance(mark, dict) else None
    if not isinstance(name, str) or not name:
        return _malformed("mark.name must be a non-empty string")
    return AgentMark(name)


def _json_object(raw: str | bytes) -> Frame | MediaFault:
    if isinstance(raw, bytes):
        return _malformed("binary frames are not supported; send JSON text frames")
    try:
        frame = orjson.loads(raw)
    except orjson.JSONDecodeError:
        return _malformed("the frame is not valid JSON")
    if not isinstance(frame, dict):
        return _malformed("the frame must be a JSON object")
    return frame


def parse_agent_frame(raw: str | bytes, fmt: MediaFormat) -> AgentFrame | MediaFault:
    """One WebSocket message from the agent; agent audio is parsed in ``fmt``."""
    frame = _json_object(raw)
    if isinstance(frame, MediaFault):
        return frame
    event = frame.get("event")
    if event == "media":
        return _parse_media(frame, fmt)
    if event == "mark":
        return _parse_mark(frame)
    if event == "clear":
        return AgentClear()
    return _malformed(f"unknown event {event!r}; expected media, mark, or clear")
