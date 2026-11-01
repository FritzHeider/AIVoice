"""Realtime STT wire format: socket URL, the audio chunk message, server message parsing (specs/08 §2-§4).

Pure functions; every client message is built here from the spec (never shared with the simulator).
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote_plus, urlencode

import orjson

from va_elevenlabs.stt_errors import ACTIONS, SttProtocolError
from va_elevenlabs.stt_models import SttAudioFormat, SttConfig, SttConnection, SttEntity, SttWord

__all__ = [
    "IGNORED_TYPES",
    "PATH",
    "Committed",
    "CommittedWords",
    "Entities",
    "Ignored",
    "Partial",
    "ServerError",
    "ServerMessage",
    "SessionStarted",
    "audio_chunk",
    "bytes_per_ms",
    "parse_message",
    "realtime_url",
    "silence_frame",
]

PATH: Final = "/v1/speech-to-text/realtime"
IGNORED_TYPES: Final = frozenset({"edited_transcript", "insufficient_audio_activity"})
_FRAME_MS: Final = 20
_ULAW_SILENCE: Final = 0xFF
_ENTITY_FIELDS: Final = ("text", "entity_type", "start_char", "end_char")


def _flag(value: bool) -> str:
    return "true" if value else "false"


def realtime_url(conn: SttConnection, cfg: SttConfig, *, minimal: bool = False) -> str:
    """``{ws_base}/v1/speech-to-text/realtime?{query}`` with the §3 query in table order; ``minimal`` drops
    keyterms and entity detection (the ``invalid_request`` retry)."""
    query: list[tuple[str, str]] = [
        ("model_id", conn.model_id),
        ("audio_format", cfg.audio_format),
        ("commit_strategy", "manual"),
        ("include_timestamps", "true"),
        ("include_language_detection", _flag(cfg.include_language_detection)),
    ]
    if cfg.language_code is not None:
        query.append(("language_code", cfg.language_code))
    keyterms = [] if minimal else cfg.keyterms
    if keyterms and conn.keyterms_encoding == "json":
        query.append(("keyterms", orjson.dumps(keyterms).decode()))
    else:
        query.extend(("keyterms", term) for term in keyterms)
    query.append(("filter_background_audio", _flag(cfg.filter_background_audio)))
    if not minimal:
        query.extend(("entity_detection", category) for category in cfg.entity_detection)
    query.append(("keepalive_interval_ms", str(conn.keepalive_interval_ms)))
    if conn.zero_retention:
        query.append(("enable_logging", "false"))
    query.append(("no_verbatim", "false"))
    return f"{conn.ws_base.rstrip('/')}{PATH}?{urlencode(query, quote_via=quote_plus)}"


def bytes_per_ms(fmt: SttAudioFormat) -> int:
    """Wire bytes per millisecond: μ-law 8 kHz is 8, PCM16 16 kHz is 32."""
    return 8 if fmt == "ulaw_8000" else 32


def silence_frame(fmt: SttAudioFormat) -> bytes:
    """One 20 ms codec-silence frame (160 bytes of ``0xFF`` for μ-law, 640 zero bytes for PCM16)."""
    size = _FRAME_MS * bytes_per_ms(fmt)
    return bytes([_ULAW_SILENCE]) * size if fmt == "ulaw_8000" else bytes(size)


def audio_chunk(audio: bytes, *, commit: bool, sample_rate: int, previous_text: str | None = None) -> str:
    """An ``input_audio_chunk`` message (key order as the §4 example; ``previous_text`` only when given)."""
    message: dict[str, Any] = {
        "message_type": "input_audio_chunk",
        "audio_base_64": base64.b64encode(audio).decode("ascii"),
        "commit": commit,
        "sample_rate": sample_rate,
    }
    if previous_text is not None:
        message["previous_text"] = previous_text
    return orjson.dumps(message).decode()


@dataclass(frozen=True, slots=True)
class SessionStarted:
    session_id: str
    audio_format: object
    sample_rate: object
    commit_strategy: object
    keyterms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Partial:
    text: str


@dataclass(frozen=True, slots=True)
class Committed:
    text: str


@dataclass(frozen=True, slots=True)
class CommittedWords:
    text: str
    language_code: str | None
    words: tuple[SttWord, ...]


@dataclass(frozen=True, slots=True)
class Entities:
    text: str
    entities: tuple[SttEntity, ...]


@dataclass(frozen=True, slots=True)
class ServerError:
    """An error or warning frame of a §4 row (``ACTIONS`` holds its handling)."""

    message_type: str
    error: str


@dataclass(frozen=True, slots=True)
class Ignored:
    """``edited_transcript``, ``insufficient_audio_activity``, or a type the spec does not list."""

    message_type: str


type ServerMessage = SessionStarted | Partial | Committed | CommittedWords | Entities | ServerError | Ignored


def _bad(why: str) -> SttProtocolError:
    return SttProtocolError("invalid_server_frame", why)


def _str(frame: dict[str, Any], key: str) -> str:
    value = frame.get(key)
    if not isinstance(value, str):
        msg = f"{frame.get('message_type')}.{key} must be a string"
        raise _bad(msg)
    return value


def _number(item: dict[str, Any], key: str) -> float:
    value = item.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"words[].{key} must be a number"
        raise _bad(msg)
    return float(value)


def _words(frame: dict[str, Any]) -> tuple[SttWord, ...]:
    items = frame.get("words", [])
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        msg = "words must be a list of objects"
        raise _bad(msg)
    words: list[SttWord] = []
    for item in items:
        if item.get("type", "word") != "word":
            continue
        logprob = item.get("logprob")
        words.append(
            SttWord(
                text=_str(item, "text"),
                start_s=_number(item, "start"),
                end_s=_number(item, "end"),
                logprob=None if logprob is None else _number(item, "logprob"),
            )
        )
    return tuple(words)


def _entities(frame: dict[str, Any]) -> tuple[SttEntity, ...]:
    items = frame.get("entities", [])
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        msg = "entities must be a list of objects"
        raise _bad(msg)
    try:
        return tuple(
            SttEntity.model_validate({key: item.get(key) for key in _ENTITY_FIELDS}, strict=True)
            for item in items
        )
    except ValueError:
        msg = "entities[] must carry text, entity_type, start_char, end_char"
        raise _bad(msg) from None


def _language(frame: dict[str, Any]) -> str | None:
    value = frame.get("language_code")
    if value is not None and not isinstance(value, str):
        msg = "language_code must be a string or null"
        raise _bad(msg)
    return value


def _session_started(frame: dict[str, Any]) -> SessionStarted:
    config = frame.get("config")
    if not isinstance(config, dict):
        msg = "session_started.config must be an object"
        raise _bad(msg)
    keyterms = config.get("keyterms", [])
    if not isinstance(keyterms, list) or not all(isinstance(term, str) for term in keyterms):
        msg = "session_started.config.keyterms must be a list of strings"
        raise _bad(msg)
    return SessionStarted(
        _str(frame, "session_id"),
        config.get("audio_format"),
        config.get("sample_rate"),
        config.get("commit_strategy"),
        tuple(keyterms),
    )


def _error(frame: dict[str, Any], message_type: str) -> ServerError:
    error = frame.get("error", "")
    if not isinstance(error, str):
        msg = f"{message_type}.error must be a string"
        raise _bad(msg)
    return ServerError(message_type, error)


_PARSERS: Final[dict[str, Callable[[dict[str, Any]], ServerMessage]]] = {
    "session_started": _session_started,
    "partial_transcript": lambda frame: Partial(_str(frame, "text")),
    "committed_transcript": lambda frame: Committed(_str(frame, "text")),
    "committed_transcript_with_timestamps": lambda frame: CommittedWords(
        _str(frame, "text"), _language(frame), _words(frame)
    ),
    "committed_transcript_entities": lambda frame: Entities(_str(frame, "text"), _entities(frame)),
}


def _typed(frame: dict[str, Any], message_type: str) -> ServerMessage:
    parser = _PARSERS.get(message_type)
    if parser is not None:
        return parser(frame)
    if message_type in ACTIONS:
        return _error(frame, message_type)
    return Ignored(message_type)


def parse_message(raw: str | bytes) -> ServerMessage:
    """Parse one server frame; malformed frames raise :class:`SttProtocolError`."""
    try:
        frame = orjson.loads(raw)
    except orjson.JSONDecodeError:
        msg = "frame is not JSON"
        raise _bad(msg) from None
    if not isinstance(frame, dict):
        msg = "frame is not a JSON object"
        raise _bad(msg)
    message_type = frame.get("message_type")
    if not isinstance(message_type, str) or not message_type:
        msg = "message_type must be a non-empty string"
        raise _bad(msg)
    return _typed(frame, message_type)
