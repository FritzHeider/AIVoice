"""Query parameters of the realtime STT WebSocket (specs/30 §3.3, specs/08 §3; Decision P03-T05a rules 2, 3).

Unknown parameters are ignored; an invalid known one raises ``ValueError`` with the ``invalid <param>: <why>``
text the socket reports in its ``invalid_request`` frame and close reason (code 1008).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Final, Literal

from starlette.datastructures import QueryParams

__all__ = [
    "AUDIO_FORMATS",
    "DEFAULT_AUDIO_FORMAT",
    "ENTITY_CATEGORIES",
    "CommitStrategy",
    "SttFormat",
    "SttParams",
    "config_json",
    "parse_stt_params",
]

type CommitStrategy = Literal["manual", "vad"]
type Encoding = Literal["ulaw", "pcm"]


@dataclass(frozen=True, slots=True)
class SttFormat:
    """An input audio format: ``ulaw`` is G.711 μ-law, ``pcm`` signed 16-bit little-endian mono."""

    name: str
    sample_rate: int
    encoding: Encoding


AUDIO_FORMATS: Final = {
    fmt.name: fmt
    for fmt in (
        SttFormat("ulaw_8000", 8_000, "ulaw"),
        *(SttFormat(f"pcm_{rate}", rate, "pcm") for rate in (8_000, 16_000, 22_050, 24_000, 44_100, 48_000)),
    )
}
DEFAULT_AUDIO_FORMAT: Final = "pcm_16000"
ENTITY_CATEGORIES: Final = frozenset({"pii", "pci", "phi", "all"})
_COMMIT_STRATEGIES: Final[dict[str, CommitStrategy]] = {"manual": "manual", "vad": "vad"}
_BOOLEANS: Final = {"true": True, "false": False}
_LANGUAGE_CODE: Final = re.compile(r"[a-z]{2,3}")
_MAX_KEYTERM_CHARS: Final = 50
_MAX_KEYTERMS: Final = 100
_KEEPALIVE_MS: Final = (1_000, 60_000)
_VAD_SILENCE_S: Final = (0.3, 3.0)
_DEFAULT_VAD_SILENCE_S: Final = 1.5


@dataclass(frozen=True, slots=True)
class SttParams:
    model_id: str
    audio_format: SttFormat
    commit_strategy: CommitStrategy
    include_timestamps: bool
    include_language_detection: bool
    language_code: str | None
    keyterms: tuple[str, ...]
    entity_detection: tuple[str, ...]
    filter_background_audio: bool
    keepalive_interval_ms: int | None
    enable_logging: bool
    no_verbatim: bool
    vad_silence_threshold_secs: float


def _invalid(param: str, why: str) -> ValueError:
    return ValueError(f"invalid {param}: {why}")


def _bool(query: QueryParams, name: str, default: bool) -> bool:
    raw = query.get(name)
    if raw is None:
        return default
    if raw.lower() not in _BOOLEANS:
        raise _invalid(name, "must be true or false")
    return _BOOLEANS[raw.lower()]


def _model_id(query: QueryParams) -> str:
    model_id = query.get("model_id")
    if not model_id:
        raise _invalid("model_id", "required")
    return model_id


def _audio_format(query: QueryParams) -> SttFormat:
    fmt = AUDIO_FORMATS.get(query.get("audio_format", DEFAULT_AUDIO_FORMAT))
    if fmt is None:
        raise _invalid("audio_format", f"must be one of {', '.join(AUDIO_FORMATS)}")
    return fmt


def _commit_strategy(query: QueryParams) -> CommitStrategy:
    strategy = _COMMIT_STRATEGIES.get(query.get("commit_strategy", "manual"))
    if strategy is None:
        raise _invalid("commit_strategy", "must be manual or vad")
    return strategy


def _language_code(query: QueryParams) -> str | None:
    code = query.get("language_code")
    if code is None:
        return None
    if _LANGUAGE_CODE.fullmatch(code) is None:
        raise _invalid("language_code", "must be 2 or 3 lowercase letters")
    return code


def _keyterms(query: QueryParams) -> tuple[str, ...]:
    terms = tuple(query.getlist("keyterms"))
    if len(terms) > _MAX_KEYTERMS:
        raise _invalid("keyterms", f"at most {_MAX_KEYTERMS} terms")
    if any(not 1 <= len(term) <= _MAX_KEYTERM_CHARS for term in terms):
        raise _invalid("keyterms", f"each term must be 1 to {_MAX_KEYTERM_CHARS} characters")
    return terms


def _entity_detection(query: QueryParams) -> tuple[str, ...]:
    categories = tuple(query.getlist("entity_detection"))
    if any(category not in ENTITY_CATEGORIES for category in categories):
        raise _invalid("entity_detection", "each value must be pii, pci, phi, or all")
    return categories


def _keepalive(query: QueryParams) -> int | None:
    raw = query.get("keepalive_interval_ms")
    if raw is None:
        return None
    low, high = _KEEPALIVE_MS
    try:
        interval = int(raw)
    except ValueError:
        raise _invalid("keepalive_interval_ms", "must be an integer") from None
    if not low <= interval <= high:
        raise _invalid("keepalive_interval_ms", f"must be between {low} and {high}")
    return interval


def _vad_silence(query: QueryParams) -> float:
    raw = query.get("vad_silence_threshold_secs")
    if raw is None:
        return _DEFAULT_VAD_SILENCE_S
    low, high = _VAD_SILENCE_S
    try:
        seconds = float(raw)
    except ValueError:
        raise _invalid("vad_silence_threshold_secs", "must be a number") from None
    if not math.isfinite(seconds) or not low <= seconds <= high:
        raise _invalid("vad_silence_threshold_secs", f"must be between {low} and {high}")
    return seconds


def parse_stt_params(query: QueryParams) -> SttParams:
    """The session's parameters from its query string."""
    return SttParams(
        model_id=_model_id(query),
        audio_format=_audio_format(query),
        commit_strategy=_commit_strategy(query),
        include_timestamps=_bool(query, "include_timestamps", False),
        include_language_detection=_bool(query, "include_language_detection", False),
        language_code=_language_code(query),
        keyterms=_keyterms(query),
        entity_detection=_entity_detection(query),
        filter_background_audio=_bool(query, "filter_background_audio", False),
        keepalive_interval_ms=_keepalive(query),
        enable_logging=_bool(query, "enable_logging", True),
        no_verbatim=_bool(query, "no_verbatim", False),
        vad_silence_threshold_secs=_vad_silence(query),
    )


def config_json(params: SttParams) -> dict[str, object]:
    """The ``session_started`` config echo (key order as in Decision P03-T05a rule 3)."""
    return {
        "model_id": params.model_id,
        "audio_format": params.audio_format.name,
        "sample_rate": params.audio_format.sample_rate,
        "commit_strategy": params.commit_strategy,
        "include_timestamps": params.include_timestamps,
        "include_language_detection": params.include_language_detection,
        "language_code": params.language_code,
        "keyterms": list(params.keyterms),
        "entity_detection": list(params.entity_detection),
        "filter_background_audio": params.filter_background_audio,
        "keepalive_interval_ms": params.keepalive_interval_ms,
        "enable_logging": params.enable_logging,
        "no_verbatim": params.no_verbatim,
        "vad_silence_threshold_secs": params.vad_silence_threshold_secs,
    }
