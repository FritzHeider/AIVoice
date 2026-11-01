"""Per-context client state: clauses, confirmation by alignment, wire ids, timers (specs/09 §8, §11;
Decision P03-T10 rules 2-4).

Pure bookkeeping, driven by :class:`va_elevenlabs.tts_ws.TtsSocket`; times are the socket clock's monotonic
seconds.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from va_domain.voices import VoiceSettings
from va_elevenlabs.tts_models import ChunkAlignment

__all__ = ["Clause", "ContextState"]


def _visible(text: str) -> int:
    return sum(1 for char in text if not char.isspace())


@dataclass(slots=True)
class Clause:
    text: str
    flush: bool
    confirmed: bool = False


@dataclass(slots=True)
class ContextState:
    """One logical context. ``wire_id`` is ``logical_id`` until the first reconnect, then ``{id}-r{n}``."""

    logical_id: str
    settings: VoiceSettings
    created_at: float
    wire_id: str = ""
    clauses: list[Clause] = field(default_factory=list)
    flushed_upto: int = 0
    ended: bool = False
    ended_at: float | None = None
    cancelled: bool = False
    close_sent: bool = False
    final_emitted: bool = False
    first_text_at: float | None = None
    first_audio_at: float | None = None
    last_audio_at: float | None = None
    progress_at: float | None = None
    characters: int = 0
    reconnects: int = 0
    _received: int = 0
    _first_on_socket: int = 0

    def __post_init__(self) -> None:
        if not self.wire_id:
            self.wire_id = self.logical_id

    def add(self, text: str, *, flush: bool) -> None:
        self.clauses.append(Clause(text, flush))
        if flush:
            self.flushed_upto = len(self.clauses)

    def mark_flushed(self) -> None:
        self.flushed_upto = len(self.clauses)

    def needs_bare_flush(self) -> bool:
        """Whether a flush message is needed to generate everything sent (the last clause had no flush)."""
        return bool(self.clauses) and not self.clauses[-1].flush

    def confirm(self, alignment: ChunkAlignment | None) -> int:
        """Count one audio chunk; returns how many clauses it newly confirmed.

        A clause is confirmed once the visible (non-whitespace) characters aligned so far exceed the visible
        length of every clause before it; any chunk confirms the first clause sent on the current socket
        (its alignment may be ``None``).
        """
        if alignment is not None:
            self._received += _visible("".join(alignment.chars))
        newly = 0
        prefix = 0
        for index, clause in enumerate(self.clauses):
            reached = index == self._first_on_socket or self._received > prefix
            if not clause.confirmed and reached:
                clause.confirmed = True
                newly += 1
            prefix += _visible(clause.text)
        return newly

    def unconfirmed(self) -> list[Clause]:
        return [clause for clause in self.clauses if not clause.confirmed]

    def all_aligned(self) -> bool:
        """Whether alignment covered every visible character sent."""
        return self._received >= sum(_visible(clause.text) for clause in self.clauses)

    def complete(self) -> bool:
        """The utterance ended and every clause produced audio."""
        return self.ended and all(clause.confirmed for clause in self.clauses)

    def awaiting_audio(self) -> bool:
        """A flushed clause has produced no audio yet."""
        return any(not clause.confirmed for clause in self.clauses[: self.flushed_upto])

    def quiet_since(self) -> float | None:
        """When the context last made progress towards its end (last audio or the end mark)."""
        marks = [mark for mark in (self.last_audio_at, self.ended_at) if mark is not None]
        return max(marks) if marks else None

    def rewire(self, reconnect: int) -> None:
        """Move to the socket opened by reconnect number ``reconnect``; the aligned count restarts at the
        first unconfirmed clause (only unconfirmed clauses are resent)."""
        self.wire_id = f"{self.logical_id}-r{reconnect}"
        self.reconnects += 1
        received = 0
        first = len(self.clauses)
        for index, clause in enumerate(self.clauses):
            if not clause.confirmed:
                first = index
                break
            received += _visible(clause.text)
        self._received = received
        self._first_on_socket = first
