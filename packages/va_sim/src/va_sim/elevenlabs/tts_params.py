"""Query parameters of the TTS multi-context WebSocket (specs/30 §3.3, specs/09 §2; Decision P03-T04 rule 3).

Unknown parameters are ignored; an invalid known one raises ``ValueError`` with the ``invalid <param>: <why>``
text the socket closes with (code 1008).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from va_sim.elevenlabs.tts_audio import OutputFormat, parse_output_format

__all__ = ["DEFAULT_MODEL_ID", "DEFAULT_OUTPUT_FORMAT", "TtsParams", "parse_params"]

DEFAULT_MODEL_ID: Final = "eleven_flash_v2_5"
DEFAULT_OUTPUT_FORMAT: Final = "mp3_44100_128"
_DEFAULT_INACTIVITY_S: Final = 20
_MAX_INACTIVITY_S: Final = 180
_BOOLEANS: Final = {"true": True, "false": False}
_BOOL_PARAMS: Final = ("sync_alignment", "auto_mode", "enable_ssml_parsing", "enable_logging")
_NORMALIZATION: Final = frozenset({"auto", "on", "off"})


@dataclass(frozen=True, slots=True)
class TtsParams:
    model_id: str
    output_format: OutputFormat
    inactivity_timeout: int
    flags: Mapping[str, bool]
    apply_text_normalization: str
    language_code: str | None


def _invalid(param: str, why: str) -> ValueError:
    return ValueError(f"invalid {param}: {why}")


def _inactivity(query: Mapping[str, str]) -> int:
    raw = query.get("inactivity_timeout")
    if raw is None:
        return _DEFAULT_INACTIVITY_S
    try:
        seconds = int(raw)
    except ValueError:
        raise _invalid("inactivity_timeout", "must be an integer") from None
    if not 1 <= seconds <= _MAX_INACTIVITY_S:
        raise _invalid("inactivity_timeout", f"must be between 1 and {_MAX_INACTIVITY_S}")
    return seconds


def _flags(query: Mapping[str, str]) -> dict[str, bool]:
    flags: dict[str, bool] = {}
    for name in _BOOL_PARAMS:
        raw = query.get(name)
        if raw is None:
            continue
        if raw.lower() not in _BOOLEANS:
            raise _invalid(name, "must be true or false")
        flags[name] = _BOOLEANS[raw.lower()]
    return flags


def parse_params(query: Mapping[str, str]) -> TtsParams:
    """The socket's parameters from its query string."""
    model_id = query.get("model_id", DEFAULT_MODEL_ID)
    if not model_id:
        raise _invalid("model_id", "must not be empty")
    try:
        output_format = parse_output_format(query.get("output_format", DEFAULT_OUTPUT_FORMAT))
    except ValueError:
        raise _invalid("output_format", "unsupported format") from None
    normalization = query.get("apply_text_normalization", "auto")
    if normalization not in _NORMALIZATION:
        raise _invalid("apply_text_normalization", "must be auto, on, or off")
    return TtsParams(
        model_id=model_id,
        output_format=output_format,
        inactivity_timeout=_inactivity(query),
        flags=_flags(query),
        apply_text_normalization=normalization,
        language_code=query.get("language_code") or None,
    )
