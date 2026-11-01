"""Multi-context stream-input wire format: socket URL, client messages, server frame parsing (specs/09 §2-§3).

Pure functions; every client message is built here from the spec (never shared with the simulator).
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote, urlencode

import orjson

from va_domain.voices import VoiceSettings
from va_elevenlabs.tts_errors import TtsProtocolError
from va_elevenlabs.tts_models import ChunkAlignment, OutputFormat, TtsConfig, TtsVoice

__all__ = [
    "KEEPALIVE_CONTEXT",
    "AudioFrame",
    "ErrorFrame",
    "FinalFrame",
    "Frame",
    "IgnoredFrame",
    "close_context_message",
    "close_socket_message",
    "flush_message",
    "init_context",
    "keepalive",
    "keepalive_init",
    "normalize_clause",
    "parse_frame",
    "socket_url",
    "text_message",
]

KEEPALIVE_CONTEXT: Final = "ka"


def socket_url(config: TtsConfig, voice: TtsVoice, output_format: OutputFormat) -> str:
    """``{ws_base}/v1/text-to-speech/{voice}/multi-stream-input?{query}`` with the §2 query in table order."""
    query: list[tuple[str, str]] = [
        ("model_id", voice.model_id),
        ("output_format", output_format),
        ("sync_alignment", "true"),
        ("auto_mode", "false"),
        ("inactivity_timeout", str(config.inactivity_timeout)),
        ("apply_text_normalization", "auto"),
    ]
    if voice.language_code is not None:
        query.append(("language_code", voice.language_code))
    query.append(("enable_ssml_parsing", "false"))
    if config.zero_retention:
        query.append(("enable_logging", "false"))
    voice_id = quote(voice.provider_voice_id, safe="")
    return f"{config.ws_base.rstrip('/')}/v1/text-to-speech/{voice_id}/multi-stream-input?{urlencode(query)}"


def _dumps(message: dict[str, Any]) -> str:
    return orjson.dumps(message).decode()


def init_context(wire_id: str, settings: VoiceSettings, schedule: tuple[int, ...]) -> str:
    """The first message of a context: a single space, the voice settings, and the chunk schedule."""
    return _dumps(
        {
            "text": " ",
            "context_id": wire_id,
            "voice_settings": {
                "stability": settings.stability,
                "similarity_boost": settings.similarity_boost,
                "style": settings.style,
                "use_speaker_boost": settings.use_speaker_boost,
                "speed": settings.speed,
            },
            "generation_config": {"chunk_length_schedule": list(schedule)},
        }
    )


def keepalive_init() -> str:
    return _dumps({"text": " ", "context_id": KEEPALIVE_CONTEXT})


def keepalive() -> str:
    return _dumps({"text": "", "context_id": KEEPALIVE_CONTEXT})


def normalize_clause(text: str) -> str:
    """``text`` ending with exactly one space; whitespace-only text is a ``ValueError``."""
    stripped = text.rstrip()
    if not stripped.strip():
        msg = "clause text must contain non-whitespace characters"
        raise ValueError(msg)
    return f"{stripped} "


def text_message(wire_id: str, text: str, *, flush: bool) -> str:
    """A clause message; ``text`` must already be normalized (:func:`normalize_clause`)."""
    message: dict[str, Any] = {"text": text, "context_id": wire_id}
    if flush:
        message["flush"] = True
    return _dumps(message)


def flush_message(wire_id: str) -> str:
    return _dumps({"context_id": wire_id, "flush": True})


def close_context_message(wire_id: str) -> str:
    return _dumps({"context_id": wire_id, "close_context": True})


def close_socket_message() -> str:
    return _dumps({"close_socket": True})


@dataclass(frozen=True, slots=True)
class AudioFrame:
    wire_id: str
    audio: bytes
    alignment: ChunkAlignment | None


@dataclass(frozen=True, slots=True)
class FinalFrame:
    wire_id: str


@dataclass(frozen=True, slots=True)
class ErrorFrame:
    error: str
    message: str
    code: int | None


@dataclass(frozen=True, slots=True)
class IgnoredFrame:
    """A JSON object carrying none of ``audio``, ``isFinal``, ``error`` (unknown future message types)."""

    keys: tuple[str, ...]


type Frame = AudioFrame | FinalFrame | ErrorFrame | IgnoredFrame


def _bad(why: str) -> TtsProtocolError:
    return TtsProtocolError("invalid_server_frame", why)


def parse_frame(raw: str | bytes) -> Frame:
    """Parse one server frame; malformed frames raise :class:`TtsProtocolError`."""
    try:
        frame = orjson.loads(raw)
    except orjson.JSONDecodeError:
        msg = "frame is not JSON"
        raise _bad(msg) from None
    if not isinstance(frame, dict):
        msg = "frame is not a JSON object"
        raise _bad(msg)
    if frame.get("isFinal") is True:
        return FinalFrame(_context_id(frame))
    if "audio" in frame:
        return AudioFrame(_context_id(frame), _audio(frame["audio"]), _alignment(frame.get("alignment")))
    if "error" in frame:
        return _error(frame)
    return IgnoredFrame(tuple(sorted(frame)))


def _context_id(frame: dict[str, Any]) -> str:
    context_id = frame.get("contextId")
    if not isinstance(context_id, str) or not context_id:
        msg = "contextId must be a non-empty string"
        raise _bad(msg)
    return context_id


def _audio(value: object) -> bytes:
    if value is None:
        return b""
    if not isinstance(value, str):
        msg = "audio must be a base64 string"
        raise _bad(msg)
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        msg = "audio is not valid base64"
        raise _bad(msg) from None


def _int_list(alignment: dict[str, Any], key: str) -> tuple[int, ...]:
    values = alignment.get(key)
    if not isinstance(values, list) or not all(type(v) is int for v in values):
        msg = f"alignment.{key} must be a list of integers"
        raise _bad(msg)
    return tuple(values)


def _alignment(value: object) -> ChunkAlignment | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        msg = "alignment must be an object or null"
        raise _bad(msg)
    chars = value.get("chars")
    if not isinstance(chars, list) or not all(isinstance(c, str) for c in chars):
        msg = "alignment.chars must be a list of strings"
        raise _bad(msg)
    starts = _int_list(value, "charStartTimesMs")
    durations = _int_list(value, "charDurationsMs")
    if not len(chars) == len(starts) == len(durations):
        msg = "alignment arrays differ in length"
        raise _bad(msg)
    return ChunkAlignment(tuple(chars), starts, durations)


def _error(frame: dict[str, Any]) -> ErrorFrame:
    error = frame["error"]
    message = frame.get("message", "")
    code = frame.get("code")
    if not isinstance(error, str) or not isinstance(message, str):
        msg = "error frames carry string error and message fields"
        raise _bad(msg)
    return ErrorFrame(error, message, code if type(code) is int else None)
