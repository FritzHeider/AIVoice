"""FakeElevenLabs STT state (Decisions P03-T05a rules 7-10, P03-T05b rules 9-10): realtime session records,
the transcript script, the armed realtime fault, the live sockets, batch transcription records, and the armed
batch fault.

Composed into ``ElevenLabsStore.stt``. Sessions are recorded once they authenticate. ``script`` holds the
texts that markerless speech segments take in order; the socket pops from it in a worker thread while control
routes append on the event loop (single ``deque`` operations are atomic under the GIL).
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Final, Literal, get_args

if TYPE_CHECKING:
    from va_sim.elevenlabs.store import LiveSocket

__all__ = [
    "BATCH_FAULT_KINDS",
    "FATAL_1008",
    "FATAL_1011",
    "STT_FAULT_KINDS",
    "BatchFault",
    "BatchFaultKind",
    "SttBatchRecord",
    "SttEndReason",
    "SttFault",
    "SttFaultKind",
    "SttSessionRecord",
    "SttStore",
    "SttTranscript",
]

type SttEndReason = Literal["disconnect", "fault", "reset"]
type BatchFaultKind = Literal["error", "rate_limited", "delay"]
type SttFaultKind = Literal[
    "error",
    "auth_error",
    "quota_exceeded",
    "commit_throttled",
    "unaccepted_terms",
    "rate_limited",
    "queue_overflow",
    "resource_exhausted",
    "session_time_limit_exceeded",
    "input_error",
    "invalid_request",
    "chunk_size_exceeded",
    "insufficient_audio_activity",
    "transcriber_error",
    "disconnect",
]

STT_FAULT_KINDS: Final[tuple[SttFaultKind, ...]] = get_args(SttFaultKind.__value__)
BATCH_FAULT_KINDS: Final[tuple[BatchFaultKind, ...]] = get_args(BatchFaultKind.__value__)
# Fault kinds that close the socket after their frame, by close code; the rest leave it open.
FATAL_1008: Final[frozenset[SttFaultKind]] = frozenset(
    {
        "auth_error",
        "quota_exceeded",
        "unaccepted_terms",
        "invalid_request",
        "rate_limited",
        "session_time_limit_exceeded",
    }
)
FATAL_1011: Final[frozenset[SttFaultKind]] = frozenset({"error", "transcriber_error", "resource_exhausted"})


@dataclass(frozen=True, slots=True)
class SttTranscript:
    text: str
    utterance_ids: list[int]
    language_code: str
    committed_ms: int


@dataclass(slots=True)
class SttSessionRecord:
    """One authenticated session as served at ``GET /sim/elevenlabs/stt/sessions``."""

    id: str
    model_id: str
    audio_format: str
    commit_strategy: str
    language_code: str | None
    keyterms: list[str]
    entity_detection: list[str]
    include_timestamps: bool
    started_ms: int
    previous_text: str | None = None
    ended_ms: int | None = None
    end_reason: SttEndReason | None = None
    audio_ms: int = 0
    voiced_ms: int = 0
    partials: int = 0
    commits: int = 0
    transcripts: list[SttTranscript] = field(default_factory=list)

    def end(self, reason: SttEndReason, now_ms: int) -> None:
        """Mark the session ended (the first end wins)."""
        if self.end_reason is None:
            self.end_reason = reason
            self.ended_ms = now_ms

    def to_json(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class SttFault:
    """An armed STT fault: fires ``remaining`` more times once a session's audio reaches ``after_ms``."""

    kind: SttFaultKind
    after_ms: int
    remaining: int

    def to_json(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SttBatchRecord:
    """One successful batch transcription as served at ``GET /sim/elevenlabs/stt/batch``."""

    transcription_id: str
    model_id: str
    source: Literal["file", "url"]
    source_url: str | None
    file_format: str
    channels: int
    use_multi_channel: bool
    multichannel_output_style: str
    diarize: bool
    num_speakers: int | None
    timestamps_granularity: str
    language_code: str | None
    keyterms: list[str]
    entity_detection: list[str]
    enable_logging: bool
    audio_duration_secs: float
    received_ms: int

    def to_json(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class BatchFault:
    """An armed batch STT fault: fires on the next ``remaining`` authenticated requests."""

    kind: BatchFaultKind
    delay_ms: int
    remaining: int

    def consume(self) -> BatchFault | None:
        """Count one firing: the fault that stays armed afterwards (``None`` once ``remaining`` hits 0)."""
        self.remaining -= 1
        return self if self.remaining > 0 else None

    def to_json(self) -> dict[str, object]:
        return asdict(self)


class SttStore:
    """Realtime STT session records, transcript script, armed fault, live sockets, and the batch state."""

    def __init__(self) -> None:
        self.sessions: list[SttSessionRecord] = []
        self.sockets: set[LiveSocket] = set()
        self.script: deque[str] = deque()
        self.fault: SttFault | None = None
        self.batch: list[SttBatchRecord] = []
        self.batch_fault: BatchFault | None = None

    def arm_fault(self, kind: SttFaultKind, *, after_ms: int, times: int) -> None:
        self.fault = SttFault(kind=kind, after_ms=after_ms, remaining=times)

    def take_fault(self, at_ms: int, *, on_commit: bool) -> SttFault | None:
        """Fire the armed fault at session audio time ``at_ms``: ``commit_throttled`` only on a commit, every
        other kind only after audio was fed."""
        fault = self.fault
        if fault is None or at_ms < fault.after_ms or (fault.kind == "commit_throttled") != on_commit:
            return None
        fault.remaining -= 1
        if fault.remaining == 0:
            self.fault = None
        return fault

    def arm_batch_fault(self, kind: BatchFaultKind, *, delay_ms: int, times: int) -> None:
        self.batch_fault = BatchFault(kind=kind, delay_ms=delay_ms, remaining=times)

    def take_batch_fault(self) -> BatchFault | None:
        """Fire the armed batch fault (each firing consumes one of its ``times``)."""
        fault = self.batch_fault
        if fault is not None:
            self.batch_fault = fault.consume()
        return fault

    def reset(self) -> None:
        """Close every live socket (code 1012), forget sessions, the script, batch records, and the faults."""
        for socket in tuple(self.sockets):
            socket.close_for_reset()
        self.sockets.clear()
        self.sessions.clear()
        self.script.clear()
        self.fault = None
        self.batch.clear()
        self.batch_fault = None
