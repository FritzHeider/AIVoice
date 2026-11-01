"""Streaming STT provider interface and data types (specs/08-stt-elevenlabs.md §1; Decision P03-T11).

``va_voice`` (which owns the engine-facing interface in the spec layout) does not exist yet, so the Protocol
lives here next to its only implementation, like :class:`va_elevenlabs.tts_models.StreamingTts`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Final, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "DEFAULT_STT_MODEL",
    "SAMPLE_RATES",
    "StreamingStt",
    "SttAudioFormat",
    "SttConfig",
    "SttConnection",
    "SttEntity",
    "SttEvent",
    "SttEventKind",
    "SttWord",
]

type SttAudioFormat = Literal["ulaw_8000", "pcm_16000"]
type SttEventKind = Literal["partial", "committed", "error", "warning"]
type KeytermsEncoding = Literal["repeated", "json"]

DEFAULT_STT_MODEL: Final = "scribe_v2_realtime"
SAMPLE_RATES: Final[dict[str, int]] = {"ulaw_8000": 8000, "pcm_16000": 16000}


class SttWord(BaseModel):
    """One recognised word with session-relative times in seconds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    start_s: float
    end_s: float
    logprob: float | None = None


class SttEntity(BaseModel):
    """A detected entity (redaction input, specs/28 §5); ``start_char``/``end_char`` index the text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    entity_type: str
    start_char: int
    end_char: int


class SttConfig(BaseModel):
    """Per-session options (specs/08 §1, §3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    audio_format: SttAudioFormat = "ulaw_8000"
    sample_rate: int = 8000
    language_code: str | None = None
    include_language_detection: bool = True
    keyterms: list[str] = Field(default_factory=list)
    filter_background_audio: bool = False
    entity_detection: list[str] = Field(default_factory=list)
    previous_text: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.sample_rate != SAMPLE_RATES[self.audio_format]:
            msg = f"sample_rate {self.sample_rate} does not match audio_format {self.audio_format}"
            raise ValueError(msg)
        if self.include_language_detection != (self.language_code is None):
            msg = "include_language_detection must be true exactly when language_code is null"
            raise ValueError(msg)
        return self


class SttEvent(BaseModel):
    """A transcript, warning, or error from the session (specs/08 §1).

    ``is_update`` marks a re-emitted committed event that adds ``entities`` to an earlier one (Decision
    P03-T11 rule 5).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: SttEventKind
    text: str = ""
    words: list[SttWord] = Field(default_factory=list)
    language_code: str | None = None
    entities: list[SttEntity] = Field(default_factory=list)
    received_at: float
    error_type: str | None = None
    is_update: bool = False


# jscpd:ignore-start  (the specs/08 §1 interface, verbatim)
class StreamingStt(Protocol):
    """specs/08 §1 streaming STT provider."""

    async def open(self, cfg: SttConfig) -> None: ...

    async def send_audio(self, audio: bytes, *, commit: bool = False) -> None: ...

    async def commit(self) -> None: ...

    def events(self) -> AsyncIterator[SttEvent]: ...

    async def close(self) -> None: ...


# jscpd:ignore-end  (the specs/08 §1 interface, verbatim)


@dataclass(frozen=True, slots=True)
class SttConnection:
    """Connection settings and timing knobs of :class:`va_elevenlabs.stt_realtime.ElevenLabsRealtimeStt`
    (seconds unless named ``_ms``)."""

    ws_base: str
    api_key: str = field(repr=False)
    model_id: str = DEFAULT_STT_MODEL
    zero_retention: bool = False
    keyterms_encoding: KeytermsEncoding = "repeated"
    keepalive_interval_ms: int = 5000
    batch_ms: int = 100
    min_commit_interval_s: float = 0.25
    timestamp_grace_s: float = 0.3
    reconnect_delays_s: tuple[float, ...] = (0.05, 0.1, 0.2)
    backoff_delays_s: tuple[float, ...] = (0.2, 0.4, 0.8)
    healthy_after_s: float = 1.0
    replay_max_s: float = 2.0
    context_chars: int = 300
    rotate_after_s: float = 1500.0
    rotate_drain_s: float = 0.5
    open_timeout_s: float = 2.0
    send_timeout_s: float = 5.0
    close_timeout_s: float = 2.0
