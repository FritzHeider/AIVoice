"""FakeTelnyx call state (specs/30-testing-and-evals.md §3.2, Decision 2026-10-29 P03-T02b).

A :class:`Call` is one leg. Only :class:`va_sim.telnyx.call_engine.CallEngine` mutates it; handlers read it to
validate commands and the control API serialises it with :meth:`Call.summary`. ``audio``, ``stream``, and
``timeline_utterances`` are the hooks the media WS client (P03-T03) fills.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from va_sim.clock import iso_z
from va_sim.scenario import Answer, TransferTarget
from va_sim.telnyx.store import Record

__all__ = [
    "CONFERENCE_LIFETIME",
    "DEFAULT_OUTCOME",
    "ActiveRecording",
    "Call",
    "CallAudio",
    "CallState",
    "CommandEntry",
    "Conference",
    "Direction",
    "DtmfEntry",
    "EventEntry",
    "LegOutcome",
    "PendingMedia",
    "Playback",
    "StreamState",
    "TransferEntry",
    "outcome_of",
]

type Direction = Literal["incoming", "outgoing"]
type CallState = Literal["bridging", "parked", "answered", "bridged", "hangup"]

CONFERENCE_LIFETIME: Final = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class LegOutcome:
    """How the far end of an outgoing leg behaves (scenario ``answer`` or ``transfer_target``)."""

    result: Literal["answered", "busy", "no_answer", "rejected", "failed"]
    ring_s: float
    answered_by: str
    beep_at_s: float | None


DEFAULT_OUTCOME: Final = LegOutcome("answered", 1.0, "human_residence", None)


def outcome_of(behaviour: Answer | TransferTarget | None) -> LegOutcome:
    """The outcome a scenario section describes; no section answers like a person after 1 s."""
    if isinstance(behaviour, Answer):
        return LegOutcome(
            behaviour.result,
            (behaviour.after_ms or 0) / 1000,
            behaviour.answered_by or "human_residence",
            None if behaviour.beep_at_ms is None else behaviour.beep_at_ms / 1000,
        )
    if isinstance(behaviour, TransferTarget):
        return LegOutcome(behaviour.result, (behaviour.answer_after_ms or 0) / 1000, "human_residence", None)
    return DEFAULT_OUTCOME


@dataclass(slots=True)
class CallAudio:
    """PCM16 8 kHz tracks: what the caller (far end) sent and what the agent (platform) sent."""

    caller: bytearray = field(default_factory=bytearray)
    agent: bytearray = field(default_factory=bytearray)


@dataclass(slots=True)
class StreamState:
    stream_id: str
    params: dict[str, object]


@dataclass(frozen=True, slots=True)
class ActiveRecording:
    recording_id: str
    channels: Literal["single", "dual"]
    format: Literal["wav", "mp3"]
    started_at: datetime
    started_ms: int
    caller_offset: int
    agent_offset: int


@dataclass(frozen=True, slots=True)
class Playback:
    media_url: str
    overlay: bool


@dataclass(frozen=True, slots=True)
class PendingMedia:
    """Streaming and recording a dial or answer command asked to start when the leg is answered."""

    stream: dict[str, object] | None = None
    record_channels: Literal["single", "dual"] | None = None
    record_format: Literal["wav", "mp3"] = "wav"


@dataclass(frozen=True, slots=True)
class CommandEntry:
    name: str
    command_id: str | None
    at: str
    duplicate: bool


@dataclass(frozen=True, slots=True)
class EventEntry:
    event_type: str
    event_id: str
    at: str


@dataclass(frozen=True, slots=True)
class DtmfEntry:
    digits: str
    duration_millis: int
    at: str


@dataclass(frozen=True, slots=True)
class TransferEntry:
    call_control_id: str
    to: str
    from_: str
    at: str


@dataclass(slots=True)
class Conference:
    record: Record
    # Participant leg → end_conference_on_exit, in join order.
    participants: dict[Call, bool] = field(default_factory=dict)

    @property
    def conference_id(self) -> str:
        conference_id: str = self.record["id"]
        return conference_id


@dataclass(slots=True, eq=False)
class Call:
    """One call leg on an account."""

    call_control_id: str
    call_leg_id: str
    call_session_id: str
    connection_id: str
    direction: Direction
    from_: str
    to: str
    webhook_url: str
    base_url: str
    created_at: datetime
    created_ms: int
    state: CallState
    client_state: str | None = None
    scenario_id: str | None = None
    outcome: LegOutcome = DEFAULT_OUTCOME
    amd: str = "disabled"
    time_limit_s: float = 14_400.0
    pending: PendingMedia = field(default_factory=PendingMedia)
    bridge_on_answer: Call | None = None
    waiting_legs: list[Call] = field(default_factory=list)
    answered_at: datetime | None = None
    answered_ms: int | None = None
    ended_at: datetime | None = None
    ended_ms: int | None = None
    hangup_cause: str | None = None
    hangup_source: str | None = None
    bridged_with: Call | None = None
    park_after_unbridge: bool = False
    stream: StreamState | None = None
    recording: ActiveRecording | None = None
    recordings: list[str] = field(default_factory=list)
    playbacks: list[Playback] = field(default_factory=list)
    conference: Conference | None = None
    commands: list[CommandEntry] = field(default_factory=list)
    command_ids: set[str] = field(default_factory=set)
    events: list[EventEntry] = field(default_factory=list)
    dtmf_sent: list[DtmfEntry] = field(default_factory=list)
    transfers: list[TransferEntry] = field(default_factory=list)
    timers: set[asyncio.Task[None]] = field(default_factory=set)
    audio: CallAudio = field(default_factory=CallAudio)
    timeline_utterances: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ended(self) -> bool:
        return self.state == "hangup"

    @property
    def answered(self) -> bool:
        """Answered and still up."""
        return self.answered_at is not None and not self.ended

    @property
    def is_alive(self) -> bool:
        return self.answered

    @property
    def platform_side(self) -> str:
        """``hangup_source`` when the platform ends this leg (it dials outgoing legs, answers incoming)."""
        return "caller" if self.direction == "outgoing" else "callee"

    @property
    def remote_side(self) -> str:
        """``hangup_source`` when the far end ends this leg."""
        return "callee" if self.direction == "outgoing" else "caller"

    def payload(self, **extra: object) -> Record:
        """The common call event payload plus ``extra``."""
        return {
            "call_control_id": self.call_control_id,
            "call_leg_id": self.call_leg_id,
            "call_session_id": self.call_session_id,
            "connection_id": self.connection_id,
            "client_state": self.client_state,
            "from": self.from_,
            "to": self.to,
            "direction": self.direction,
            "state": self.state,
            "start_time": iso_z(self.created_at),
            **extra,
        }

    def participant(self, conference: Conference, **extra: object) -> Record:
        """A conference event payload for this leg."""
        return {
            "conference_id": conference.conference_id,
            "call_control_id": self.call_control_id,
            "call_leg_id": self.call_leg_id,
            "call_session_id": self.call_session_id,
            "client_state": self.client_state,
            "connection_id": self.connection_id,
            **extra,
        }

    def summary(self) -> Record:
        """``GET /sim/telnyx/calls/{ccid}``."""
        return {
            "call_control_id": self.call_control_id,
            "call_leg_id": self.call_leg_id,
            "call_session_id": self.call_session_id,
            "connection_id": self.connection_id,
            "direction": self.direction,
            "from": self.from_,
            "to": self.to,
            "state": self.state,
            "is_alive": self.is_alive,
            "scenario_id": self.scenario_id,
            "client_state": self.client_state,
            "created_at": iso_z(self.created_at),
            "answered_at": _iso(self.answered_at),
            "ended_at": _iso(self.ended_at),
            "hangup_cause": self.hangup_cause,
            "hangup_source": self.hangup_source,
            "commands": [
                {"name": c.name, "command_id": c.command_id, "at": c.at, "duplicate": c.duplicate}
                for c in self.commands
            ],
            "events": [{"event_type": e.event_type, "event_id": e.event_id, "at": e.at} for e in self.events],
            "dtmf_sent": [
                {"digits": d.digits, "duration_millis": d.duration_millis, "at": d.at} for d in self.dtmf_sent
            ],
            "transfers": [
                {"call_control_id": t.call_control_id, "to": t.to, "from": t.from_, "at": t.at}
                for t in self.transfers
            ],
            "recordings": list(self.recordings),
            "stream": None
            if self.stream is None
            else {"stream_id": self.stream.stream_id, **self.stream.params},
            "conference_id": None if self.conference is None else self.conference.conference_id,
        }

    def timeline(self) -> Record:
        """``GET /sim/telnyx/calls/{ccid}/timeline`` (sim ``virtual_ms``)."""
        return {
            "call_control_id": self.call_control_id,
            "initiated_ms": self.created_ms,
            "answered_ms": self.answered_ms,
            "ended_ms": self.ended_ms,
            "utterances": list(self.timeline_utterances),
        }


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else iso_z(moment)
