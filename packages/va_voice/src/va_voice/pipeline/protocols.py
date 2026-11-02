"""Structural interfaces of the call pipeline (specs/07-media-pipeline.md §1, §5.2, §16, Decision P04-T12a).

The pipeline never imports a vendor client: the transport, STT stream, TTS stream, VAD and Brain are
protocols. Their members are read-only properties (or methods) so that the ``va_elevenlabs`` objects
(``SttEvent``, ``TtsAudio``, ``TtsSocket``, ``ElevenLabsRealtimeStt``) and ``va_voice.vad.SileroVad`` satisfy
them as they are.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

from va_voice.outbound import OutboundSink

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    from va_voice.latency import TurnReport
    from va_voice.phrase_cache import CachedPhrase
    from va_voice.pipeline.models import (
        AgentTurn,
        BrainAction,
        BrainOutput,
        BrainTiming,
        InterruptRecord,
        UserTurn,
    )
    from va_voice.text.alignment import ChunkAlignmentLike
    from va_voice.vad import VadEvent

__all__ = [
    "NULL_OBSERVER",
    "ActionHandler",
    "Brain",
    "InboundAudio",
    "InboundDtmf",
    "MarkEcho",
    "PhraseSource",
    "PipelineObserver",
    "SttEventLike",
    "SttStream",
    "SttWordLike",
    "Transport",
    "TransportEvent",
    "TtsChunkLike",
    "TtsStream",
    "VoiceActivityDetector",
]


@dataclass(frozen=True, slots=True)
class InboundAudio:
    """One inbound media frame in the leg's wire format; ``chunk`` is the transport sequence number."""

    chunk: int
    payload: bytes


@dataclass(frozen=True, slots=True)
class InboundDtmf:
    """One DTMF digit pressed by the caller."""

    digit: str


@dataclass(frozen=True, slots=True)
class MarkEcho:
    """The far end played everything sent before the mark ``name``."""

    name: str


type TransportEvent = InboundAudio | InboundDtmf | MarkEcho


class Transport(OutboundSink, Protocol):
    """The call leg's media socket (Telnyx media stream, widget WebSocket, test fake)."""

    def events(self) -> AsyncIterator[TransportEvent]:
        """Inbound events in arrival order; the iterator ends when the far end closed the stream."""
        ...

    async def clear(self) -> None:
        """Drop the audio queued at the far end (barge-in)."""
        ...


class SttWordLike(Protocol):
    @property
    def text(self) -> str: ...


class SttEventLike(Protocol):
    """A realtime STT event; ``kind``: ``partial``, ``committed``, ``error``, ``warning`` (others ignored)."""

    @property
    def kind(self) -> str: ...

    @property
    def text(self) -> str: ...

    @property
    def words(self) -> Sequence[SttWordLike]: ...

    @property
    def language_code(self) -> str | None: ...

    @property
    def is_update(self) -> bool:
        """A re-emitted committed transcript that only adds data to an earlier one."""
        ...


class SttStream(Protocol):
    """A realtime STT session committed manually by the endpointer (specs/08)."""

    async def send_audio(self, audio: bytes) -> None: ...

    async def commit(self) -> None: ...

    def events(self) -> AsyncIterator[SttEventLike]: ...


class TtsChunkLike(Protocol):
    """Audio of one TTS context (in the TTS output format), or its end (``is_final``)."""

    @property
    def context_id(self) -> str: ...

    @property
    def audio(self) -> bytes: ...

    @property
    def alignment(self) -> ChunkAlignmentLike | None: ...

    @property
    def is_final(self) -> bool: ...


class TtsStream(Protocol):
    """A multi-context streaming TTS socket (specs/09)."""

    async def start_context(self, context_id: str) -> None: ...

    async def send(self, context_id: str, text: str, *, flush: bool = False) -> None: ...

    async def flush(self, context_id: str) -> None: ...

    async def cancel(self, context_id: str) -> None: ...

    def audio(self) -> AsyncIterator[TtsChunkLike]: ...


class PhraseSource(Protocol):
    """Pre-rendered phrases of the call's voice (specs/07 §11 step 5, Decision P05-T10d).

    ``text`` is the phrase after the pronunciation guide (what the TTS context would be sent); a phrase that
    is not stored is ``None``. The Speaker plays a hit instead of opening a TTS context.
    """

    async def lookup(self, text: str) -> CachedPhrase | None: ...


class VoiceActivityDetector(Protocol):
    """VAD of one leg fed with 16 kHz PCM16; event times are stream time (ms of audio fed)."""

    async def feed(self, pcm16k: NDArray[np.int16]) -> list[VadEvent]: ...


class Brain(Protocol):
    """The conversation logic as the pipeline sees it (va_media adapts ``ConversationEngine`` to it, §16).

    DTMF reaches the Brain as a :class:`~va_voice.pipeline.models.UserTurn` with ``dtmf`` set (§13).
    """

    def start(self, greeting: UserTurn | None = None) -> AsyncIterator[BrainOutput]:
        """The opener (§11 step 5); with ``wait_for_greeting`` (§11 step 4) ``greeting`` is the callee's
        first turn, or ``None`` when ``greeting_timeout_ms`` passed in silence (and without
        ``wait_for_greeting``)."""
        ...

    def on_user_turn(self, turn: UserTurn) -> AsyncIterator[BrainOutput]: ...

    def on_no_input(self) -> AsyncIterator[BrainOutput]:
        """The no-input timer expired (§12).

        An empty stream lets the pipeline speak the node's ``no_input_prompt`` (or the default reprompt).
        """
        ...

    async def on_interrupted(self, spoken_text: str) -> None: ...

    def timing(self) -> BrainTiming:
        """Timing settings of the current node."""
        ...


class ActionHandler(Protocol):
    """Executes the telephony outputs the pipeline does not interpret (transfer, SMS, DTMF, hold, ...)."""

    async def handle(self, action: BrainAction) -> None: ...


class PipelineObserver(Protocol):
    """Synchronous callbacks for turn records, barge-ins and latency reports (persistence and events live
    outside). ``on_user_turn`` fires when a turn is handed to the Brain (held turns once merged, a superseding
    turn again).
    """

    def on_user_turn(self, turn: UserTurn) -> None: ...

    def on_agent_turn(self, turn: AgentTurn) -> None: ...

    def on_latency(self, report: TurnReport) -> None: ...

    def on_interrupt(self, record: InterruptRecord) -> None: ...


class _NullObserver:
    def on_user_turn(self, turn: UserTurn) -> None:
        del turn

    def on_agent_turn(self, turn: AgentTurn) -> None:
        del turn

    def on_latency(self, report: TurnReport) -> None:
        del report

    def on_interrupt(self, record: InterruptRecord) -> None:
        del record


NULL_OBSERVER: Final[PipelineObserver] = _NullObserver()
