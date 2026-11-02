"""Telnyx media WebSocket protocol models (specs/06-telephony-telnyx.md §8; specs/06 §4.1 Decision P03-T09
rule 4).

Inbound frames are parsed with ``orjson.loads`` and a hand-written dispatch on ``event``: ``media`` frames (50
per second per call) skip Pydantic and become a ``__slots__`` :class:`MediaFrame`; the rare control frames are
validated by the §8 models, which ignore unknown fields. An unknown ``event`` is an
:class:`UnknownMediaEvent`, never an error; malformed JSON or a frame missing a required field raises
:class:`MediaProtocolError`. Outbound builders return the exact JSON bytes to send.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from typing import Any, Final, Literal

import orjson
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "InboundMediaMessage",
    "MediaConnected",
    "MediaDtmf",
    "MediaDtmfInfo",
    "MediaError",
    "MediaErrorCode",
    "MediaErrorInfo",
    "MediaFormat",
    "MediaFrame",
    "MediaMark",
    "MediaMarkInfo",
    "MediaProtocolError",
    "MediaStart",
    "MediaStartInfo",
    "MediaStop",
    "MediaStopInfo",
    "UnknownMediaEvent",
    "clear_message",
    "mark_message",
    "media_message",
    "parse_media_message",
    "parse_segment_mark",
    "segment_mark",
]


class MediaProtocolError(ValueError):
    """An inbound media WebSocket message that is not a valid Telnyx frame."""


class MediaErrorCode(IntEnum):
    UNKNOWN_ERROR = 100002
    MALFORMED_FRAME = 100003
    INVALID_MEDIA = 100004
    RATE_LIMIT_REACHED = 100005


class _Frame(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)


class MediaConnected(_Frame):
    event: Literal["connected"]
    version: str


class MediaFormat(_Frame):
    encoding: Literal["PCMU", "PCMA", "G722", "OPUS", "AMR-WB", "L16"]
    sample_rate: int
    channels: int


class MediaStartInfo(_Frame):
    user_id: str | None = None
    call_control_id: str
    call_session_id: str | None = None
    from_: str | None = Field(None, validation_alias="from")
    to: str | None = None
    tags: tuple[str, ...] = ()
    client_state: str | None = None
    media_format: MediaFormat


class MediaStart(_Frame):
    event: Literal["start"]
    sequence_number: str
    start: MediaStartInfo
    stream_id: str


class MediaDtmfInfo(_Frame):
    digit: str


class MediaDtmf(_Frame):
    event: Literal["dtmf"]
    stream_id: str
    occurred_at: datetime
    sequence_number: str
    dtmf: MediaDtmfInfo


class MediaStopInfo(_Frame):
    user_id: str | None = None
    call_control_id: str


class MediaStop(_Frame):
    event: Literal["stop"]
    sequence_number: str
    stop: MediaStopInfo
    stream_id: str


class MediaErrorInfo(_Frame):
    code: int
    title: str
    detail: str | None = None


class MediaError(_Frame):
    event: Literal["error"]
    payload: MediaErrorInfo
    stream_id: str | None = None


class MediaMarkInfo(_Frame):
    name: str


class MediaMark(_Frame):
    event: Literal["mark"]
    stream_id: str
    sequence_number: str
    mark: MediaMarkInfo


@dataclass(frozen=True, slots=True)
class MediaFrame:
    """A caller audio chunk: RTP payload bytes (no header), Telnyx ``chunk`` number, ms ``timestamp``, and the
    ``track`` (``inbound`` on primary legs; ``None`` when absent or not a string)."""

    chunk: int
    timestamp: int
    payload: bytes
    track: str | None = None


@dataclass(frozen=True, slots=True)
class UnknownMediaEvent:
    """A frame whose ``event`` we do not handle."""

    event: str
    raw: dict[str, Any]


type InboundMediaMessage = (
    MediaFrame
    | MediaConnected
    | MediaStart
    | MediaDtmf
    | MediaStop
    | MediaError
    | MediaMark
    | UnknownMediaEvent
)

type _ControlModel = (
    type[MediaConnected]
    | type[MediaStart]
    | type[MediaDtmf]
    | type[MediaStop]
    | type[MediaError]
    | type[MediaMark]
)

_CONTROL_MODELS: Final[Mapping[str, _ControlModel]] = {
    "connected": MediaConnected,
    "start": MediaStart,
    "dtmf": MediaDtmf,
    "stop": MediaStop,
    "error": MediaError,
    "mark": MediaMark,
}


def _media_frame(frame: dict[str, Any]) -> MediaFrame:
    media = frame["media"]
    chunk, timestamp = int(media["chunk"]), int(media["timestamp"])  # TypeError when media is not an object
    track = media.get("track")
    return MediaFrame(
        chunk=chunk,
        timestamp=timestamp,
        payload=base64.b64decode(media["payload"], validate=True),
        track=track if isinstance(track, str) else None,
    )


def _control_frame(model: _ControlModel) -> Callable[[dict[str, Any]], InboundMediaMessage]:
    def parse(frame: dict[str, Any]) -> InboundMediaMessage:
        return model.model_validate(frame)

    return parse


_PARSERS: Final[Mapping[str, Callable[[dict[str, Any]], InboundMediaMessage]]] = {
    "media": _media_frame,
    **{event: _control_frame(model) for event, model in _CONTROL_MODELS.items()},
}


def parse_media_message(raw: str | bytes) -> InboundMediaMessage:
    """Parse one inbound text message of the media WebSocket."""
    try:
        frame = orjson.loads(raw)
    except orjson.JSONDecodeError as exc:
        msg = "the media message is not JSON"
        raise MediaProtocolError(msg) from exc
    if not isinstance(frame, dict) or not isinstance(frame.get("event"), str):
        msg = "the media message is not an object with an event"
        raise MediaProtocolError(msg)
    event: str = frame["event"]
    parser = _PARSERS.get(event)
    if parser is None:
        return UnknownMediaEvent(event, frame)
    try:
        return parser(frame)
    except (KeyError, TypeError, ValueError) as exc:
        # ValueError covers int() of a bad chunk, binascii.Error (bad base64), and pydantic ValidationError.
        msg = f"malformed {event} frame: {type(exc).__name__}"
        raise MediaProtocolError(msg) from exc


# Outbound (us -> Telnyx).


def media_message(payload: bytes) -> bytes:
    """``{"event": "media", "media": {"payload": <base64>}}`` for PCMU or L16 bytes (20 ms to 30 s)."""
    if not payload:
        msg = "the media payload is empty"
        raise ValueError(msg)
    return orjson.dumps({"event": "media", "media": {"payload": base64.b64encode(payload).decode("ascii")}})


def mark_message(name: str) -> bytes:
    """``{"event": "mark", "mark": {"name": name}}``; Telnyx echoes the mark when playback reaches it."""
    if not name:
        msg = "the mark name is empty"
        raise ValueError(msg)
    return orjson.dumps({"event": "mark", "mark": {"name": name}})


_CLEAR: Final = orjson.dumps({"event": "clear"})


def clear_message() -> bytes:
    """``{"event": "clear"}``: drop every queued outbound audio chunk (barge-in)."""
    return _CLEAR


_SEGMENT_MARK: Final = re.compile(r"seg:(\d+):(\d+)")


def segment_mark(turn_index: int, segment_index: int) -> str:
    """The mark name of a TTS segment: ``seg:<turn_index>:<segment_index>``."""
    if turn_index < 0 or segment_index < 0:
        msg = "turn and segment indexes must not be negative"
        raise ValueError(msg)
    return f"seg:{turn_index}:{segment_index}"


def parse_segment_mark(name: str) -> tuple[int, int] | None:
    """``(turn_index, segment_index)`` of a :func:`segment_mark` name; ``None`` for any other mark."""
    match = _SEGMENT_MARK.fullmatch(name)
    if match is None:
        return None
    return int(match[1]), int(match[2])
