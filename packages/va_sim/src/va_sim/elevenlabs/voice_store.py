"""FakeElevenLabs voice registry (Decision P03-T05c rules 1, 6, 8, 10-12): voices, mutation records, the
subscription limits, and the armed voices fault.

Composed into ``ElevenLabsStore.voices``. The registry is seeded from ``voice_catalog.PREMADE_VOICES`` on
construction and on reset. ``seq`` is the creation sequence (premade seed order, then clones); it is the
``created_at_unix`` sort order. Only successful mutations are recorded.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Final, Literal

from va_sim.elevenlabs.stt_store import BatchFault, BatchFaultKind
from va_sim.elevenlabs.voice_catalog import DEFAULT_VOICE_SETTINGS, PREMADE_VOICES, VerifiedLanguage

__all__ = [
    "DEFAULT_CHARACTER_LIMIT",
    "DEFAULT_MAX_VOICE_ADD_EDITS",
    "DEFAULT_TIER",
    "DEFAULT_VOICE_LIMIT",
    "SampleFile",
    "Subscription",
    "VoiceCategory",
    "VoiceChange",
    "VoiceOperation",
    "VoiceRecord",
    "VoiceRequest",
    "VoiceSample",
    "VoiceStore",
]

type VoiceCategory = Literal["premade", "cloned"]
type VoiceOperation = Literal["add", "edit", "delete", "settings_edit"]

DEFAULT_TIER: Final = "creator"
DEFAULT_CHARACTER_LIMIT: Final = 100_000
DEFAULT_VOICE_LIMIT: Final = 30
DEFAULT_MAX_VOICE_ADD_EDITS: Final = 95
_REQUEST_PREFIX: Final = "voicereq_"


@dataclass(frozen=True, slots=True)
class SampleFile:
    """A measured upload: what a request record lists for each file."""

    file_name: str
    mime_type: str
    size_bytes: int
    hash: str
    duration_secs: float | None

    def to_json(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class VoiceSample:
    """A stored sample of a cloned voice."""

    sample_id: str
    file: SampleFile

    def to_json(self) -> dict[str, object]:
        return {"sample_id": self.sample_id, **self.file.to_json()}


@dataclass(slots=True)
class VoiceRecord:
    voice_id: str
    name: str
    category: VoiceCategory
    description: str | None
    labels: dict[str, str]
    seq: int
    languages: tuple[VerifiedLanguage, ...] = ()
    samples: list[VoiceSample] | None = None
    created_at_unix: int | None = None
    settings: dict[str, float | bool] = field(default_factory=lambda: dict(DEFAULT_VOICE_SETTINGS))
    remove_background_noise: bool = False
    edits: int = 0


@dataclass(frozen=True, slots=True)
class VoiceRequest:
    """One successful mutation as served at ``GET /sim/elevenlabs/voices/requests``."""

    id: str
    operation: VoiceOperation
    voice_id: str
    name: str | None
    description: str | None
    labels: dict[str, str] | None
    files: list[SampleFile]
    remove_background_noise: bool
    settings: dict[str, float | bool] | None
    received_ms: int

    def to_json(self) -> dict[str, object]:
        body = asdict(self)
        body["files"] = [file.to_json() for file in self.files]
        return body


@dataclass(slots=True)
class Subscription:
    """The limits ``GET /v1/user/subscription`` reports (only ``voice_limit`` is enforced)."""

    tier: str = DEFAULT_TIER
    character_limit: int = DEFAULT_CHARACTER_LIMIT
    voice_limit: int = DEFAULT_VOICE_LIMIT
    max_voice_add_edits: int = DEFAULT_MAX_VOICE_ADD_EDITS


@dataclass(frozen=True, slots=True)
class VoiceChange:
    """The fields an add or edit sets; ``labels`` / ``samples`` ``None`` on an edit keep the stored ones."""

    name: str
    description: str | None
    labels: dict[str, str] | None
    samples: list[VoiceSample] | None
    remove_background_noise: bool


class VoiceStore:
    """The voice registry, mutation records, subscription limits, add/edit counter, and armed fault."""

    def __init__(self) -> None:
        self._voices: dict[str, VoiceRecord] = {}
        self._seq = 0
        self.requests: list[VoiceRequest] = []
        self.subscription = Subscription()
        self.add_edit_counter = 0
        self.fault: BatchFault | None = None
        self._seed()

    def _seed(self) -> None:
        for voice in PREMADE_VOICES:
            self._insert(
                VoiceRecord(
                    voice_id=voice.voice_id,
                    name=voice.name,
                    category="premade",
                    description=voice.description,
                    labels=dict(voice.labels),
                    seq=self._next_seq(),
                    languages=voice.languages,
                )
            )

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _insert(self, record: VoiceRecord) -> VoiceRecord:
        self._voices[record.voice_id] = record
        return record

    def __len__(self) -> int:
        return len(self._voices)

    def get(self, voice_id: str) -> VoiceRecord | None:
        return self._voices.get(voice_id)

    def ordered(self) -> list[VoiceRecord]:
        """Every voice in creation sequence."""
        return sorted(self._voices.values(), key=lambda record: record.seq)

    def cloned_count(self) -> int:
        return sum(record.category == "cloned" for record in self._voices.values())

    def add(self, voice_id: str, change: VoiceChange, *, created_at_unix: int) -> VoiceRecord:
        """A new cloned voice (counts as one add/edit)."""
        self.add_edit_counter += 1
        return self._insert(
            VoiceRecord(
                voice_id=voice_id,
                name=change.name,
                category="cloned",
                description=change.description,
                labels=dict(change.labels or {}),
                seq=self._next_seq(),
                samples=list(change.samples or []),
                created_at_unix=created_at_unix,
                remove_background_noise=change.remove_background_noise,
            )
        )

    def edit(self, record: VoiceRecord, change: VoiceChange) -> None:
        """Apply an edit; new samples replace the whole set and count as one add/edit."""
        record.name = change.name
        record.description = change.description
        record.remove_background_noise = change.remove_background_noise
        record.edits += 1
        if change.labels is not None:
            record.labels = dict(change.labels)
        if change.samples is not None:
            record.samples = list(change.samples)
            self.add_edit_counter += 1

    def delete(self, voice_id: str) -> None:
        del self._voices[voice_id]

    def edit_settings(self, record: VoiceRecord, sent: dict[str, float | bool]) -> dict[str, float | bool]:
        """Merge ``sent`` into the stored settings; the merged settings."""
        record.settings.update(sent)
        return dict(record.settings)

    def record(
        self,
        operation: VoiceOperation,
        voice_id: str,
        *,
        received_ms: int,
        change: VoiceChange | None = None,
        settings: dict[str, float | bool] | None = None,
    ) -> None:
        samples = change.samples if change is not None and change.samples is not None else []
        self.requests.append(
            VoiceRequest(
                id=f"{_REQUEST_PREFIX}{len(self.requests) + 1}",
                operation=operation,
                voice_id=voice_id,
                name=change.name if change is not None else None,
                description=change.description if change is not None else None,
                labels=change.labels if change is not None else None,
                files=[sample.file for sample in samples],
                remove_background_noise=change.remove_background_noise if change is not None else False,
                settings=settings,
                received_ms=received_ms,
            )
        )

    def arm_fault(self, kind: BatchFaultKind, *, delay_ms: int, times: int) -> None:
        self.fault = BatchFault(kind=kind, delay_ms=delay_ms, remaining=times)

    def take_fault(self) -> BatchFault | None:
        """Fire the armed voices fault (each firing consumes one of its ``times``)."""
        fault = self.fault
        if fault is not None:
            self.fault = fault.consume()
        return fault

    def reset(self) -> None:
        """Reseed the premade voices; forget clones, records, the fault, and subscription changes."""
        self._voices.clear()
        self._seq = 0
        self.requests.clear()
        self.subscription = Subscription()
        self.add_edit_counter = 0
        self.fault = None
        self._seed()
