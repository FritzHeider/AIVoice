"""Commit bookkeeping of the realtime STT client: the 250 ms throttle window, outstanding commits in FIFO
order, and the 300 ms fallback from ``committed_transcript`` to its timestamped variant (specs/08 §4;
Decision P03-T11 rules 4 and 5). Pure, no I/O; times are clock-monotonic seconds."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

__all__ = ["Awaiting", "CommitTracker", "PendingCommit", "Resolved"]


@dataclass(frozen=True, slots=True)
class PendingCommit:
    """A commit sent and not yet answered: when the engine asked for it, and its replay mark."""

    requested_at: float
    mark: int


@dataclass(frozen=True, slots=True)
class Awaiting:
    """A ``committed_transcript`` waiting for its timestamped variant until ``deadline``."""

    commit: PendingCommit | None
    text: str
    deadline: float


@dataclass(frozen=True, slots=True)
class Resolved:
    """The commit a timestamped transcript answers (``None``: the server committed on its own)."""

    commit: PendingCommit | None


class CommitTracker:
    def __init__(self, min_interval_s: float, grace_s: float) -> None:
        self._interval = min_interval_s
        self._grace = grace_s
        self._last_sent: float | None = None
        self._deferred: float | None = None
        self._deferred_requested = 0.0
        self._outstanding: deque[PendingCommit] = deque()
        self._awaiting: deque[Awaiting] = deque()
        self._late = 0

    @property
    def outstanding(self) -> int:
        return len(self._outstanding)

    @property
    def deferred(self) -> float | None:
        """When the deferred commit is due (``None``: none deferred)."""
        return self._deferred

    @property
    def last_mark(self) -> int | None:
        """The replay mark of the newest outstanding commit."""
        return self._outstanding[-1].mark if self._outstanding else None

    # Sending.

    def request(self, now: float) -> float | None:
        """A commit is wanted at ``now``: ``None`` = send it now, else the deadline it was deferred or
        merged to."""
        if self._deferred is not None:
            return self._deferred
        if self._last_sent is None or now - self._last_sent >= self._interval:
            return None
        self._defer(self._last_sent + self._interval, now)
        return self._deferred

    def take_due(self, now: float) -> float | None:
        """The request time of the deferred commit when it is due at ``now`` (it is no longer deferred)."""
        if self._deferred is None or self._deferred > now:
            return None
        self._deferred = None
        return self._deferred_requested

    def sent(self, now: float, mark: int, requested_at: float) -> None:
        self._last_sent = now
        self._outstanding.append(PendingCommit(requested_at, mark))

    def throttled(self, now: float) -> float:
        """The server dropped the newest commit (``commit_throttled``): defer a resend by one interval."""
        requested = self._outstanding.pop().requested_at if self._outstanding else now
        if self._deferred is None:
            deadline = now + self._interval
            self._defer(deadline, requested)
            return deadline
        self._deferred_requested = min(self._deferred_requested, requested)
        return self._deferred

    def collapse(self, now: float) -> None:
        """After a reconnect every outstanding commit is re-sent as one (earliest request, newest mark)."""
        if self._outstanding:
            first, last = self._outstanding[0], self._outstanding[-1]
            self._outstanding.clear()
            self._outstanding.append(PendingCommit(first.requested_at, last.mark))
            self._last_sent = now

    def _defer(self, deadline: float, requested: float) -> None:
        self._deferred = deadline
        self._deferred_requested = requested

    # Answers.

    def plain(self, text: str, now: float) -> None:
        """A ``committed_transcript`` arrived: wait ``grace_s`` for its timestamped variant."""
        commit = self._outstanding.popleft() if self._outstanding else None
        self._awaiting.append(Awaiting(commit, text, now + self._grace))

    def with_words(self) -> Resolved | None:
        """A timestamped transcript arrived: the commit it answers, or ``None`` when it is late (its plain
        variant was already emitted) and must be dropped."""
        if self._awaiting:
            return Resolved(self._awaiting.popleft().commit)
        if self._late:
            self._late -= 1
            return None
        return Resolved(self._outstanding.popleft() if self._outstanding else None)

    def expired(self, now: float) -> list[Awaiting]:
        """Plain transcripts whose grace ran out (emit them without words)."""
        due: list[Awaiting] = []
        while self._awaiting and self._awaiting[0].deadline <= now:
            due.append(self._awaiting.popleft())
        self._late += len(due)
        return due

    def flush_awaiting(self) -> list[Awaiting]:
        """Every waiting plain transcript (the socket that would send the variant is gone)."""
        due = list(self._awaiting)
        self._awaiting.clear()
        return due

    def next_deadline(self) -> float | None:
        candidates = (self._deferred, self._awaiting[0].deadline if self._awaiting else None)
        deadlines = [deadline for deadline in candidates if deadline is not None]
        return min(deadlines) if deadlines else None
