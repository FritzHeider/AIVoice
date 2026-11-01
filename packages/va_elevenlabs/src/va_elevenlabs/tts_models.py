"""TTS provider interface and data types (specs/09-tts-elevenlabs.md §1, §2; Decision P03-T10)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from va_domain.voices import VoiceSettings

__all__ = [
    "DEFAULT_CHUNK_LENGTH_SCHEDULE",
    "DEFAULT_TTS_MODEL",
    "ChunkAlignment",
    "OutputFormat",
    "StreamingTts",
    "TtsAudio",
    "TtsConfig",
    "TtsVoice",
]

type OutputFormat = Literal["ulaw_8000", "pcm_16000"]

DEFAULT_TTS_MODEL = "eleven_flash_v2_5"
DEFAULT_CHUNK_LENGTH_SCHEDULE: tuple[int, ...] = (50, 90, 120, 150)


class TtsVoice(BaseModel):
    """The voice a socket (and a context's ``voice_settings``) speaks with."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_voice_id: str = Field(min_length=1)
    model_id: str = DEFAULT_TTS_MODEL
    settings: VoiceSettings = Field(default_factory=VoiceSettings)
    language_code: str | None = None


@dataclass(frozen=True, slots=True)
class ChunkAlignment:
    """Character timings of one audio chunk, relative to the chunk start (specs/09 §7)."""

    chars: tuple[str, ...]
    char_start_ms: tuple[int, ...]
    char_duration_ms: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class TtsAudio:
    """Decoded audio of a context in the socket's output format, or its end (``is_final``, empty audio)."""

    context_id: str
    audio: bytes
    alignment: ChunkAlignment | None
    is_final: bool = False


# jscpd:ignore-start  (the specs/09 §1 interface, verbatim)
class StreamingTts(Protocol):
    """specs/09 §1 streaming TTS provider."""

    async def open(self, output_format: OutputFormat, voice: TtsVoice) -> None: ...

    async def start_context(self, context_id: str, voice: TtsVoice | None = None) -> None: ...

    async def send(self, context_id: str, text: str, *, flush: bool = False) -> None: ...

    async def flush(self, context_id: str) -> None: ...

    async def cancel(self, context_id: str) -> None: ...

    def audio(self) -> AsyncIterator[TtsAudio]: ...

    async def close(self) -> None: ...


# jscpd:ignore-end  (the specs/09 §1 interface, verbatim)


@dataclass(frozen=True, slots=True)
class TtsConfig:
    """Connection settings and timing knobs of :class:`va_elevenlabs.tts_ws.TtsSocket` (seconds)."""

    ws_base: str
    api_key: str = field(repr=False)
    zero_retention: bool = False
    inactivity_timeout: int = 180
    chunk_length_schedule: tuple[int, ...] = DEFAULT_CHUNK_LENGTH_SCHEDULE
    keepalive_s: float = 15.0
    no_audio_timeout_s: float = 3.0
    tail_grace_s: float = 0.3
    reconnect_delays_s: tuple[float, ...] = (0.05, 0.1, 0.2)
    open_timeout_s: float = 5.0
    send_timeout_s: float = 5.0
    close_timeout_s: float = 2.0
