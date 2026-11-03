"""Seq ordering of a call's live events (specs/19-events-and-webhooks.md §9 Decision P06-T06a).

Live events can arrive out of order (concurrent publishers) or after a gap (published before the stream
subscribed but not yet in ``call_events``). :class:`SeqOrder` releases items strictly in increasing seq:
an offered seq is held until every seq below it was offered or explicitly skipped with :meth:`advance`.
An offer with item ``None`` (an event that has no frame) still closes its place in the sequence.
"""

from __future__ import annotations

__all__ = ["SeqOrder"]


class SeqOrder[T]:
    """Releases items in seq order; :attr:`last` is the highest seq released or skipped."""

    def __init__(self, after: int) -> None:
        self.last = after
        self._pending: dict[int, T | None] = {}

    @property
    def has_gap(self) -> bool:
        """Whether some offered seq waits for a lower one."""
        return bool(self._pending)

    @property
    def lowest_pending(self) -> int | None:
        """The lowest held seq (``None`` without a gap)."""
        return min(self._pending, default=None)

    def offer(self, seq: int, item: T | None) -> list[T]:
        """Offer ``seq``; returns the items now releasable (duplicates and ``seq <= last`` are dropped)."""
        if seq <= self.last or seq in self._pending:
            return []
        self._pending[seq] = item
        return self._release_contiguous()

    def advance(self, seq: int) -> list[T]:
        """Give up waiting for the seqs up to ``seq``: held items up to it are released in order, and so is
        whatever then follows contiguously."""
        released: list[T] = []
        for held in sorted(s for s in self._pending if s <= seq):
            item = self._pending.pop(held)
            if item is not None:
                released.append(item)
        self.last = max(self.last, seq)
        return released + self._release_contiguous()

    def _release_contiguous(self) -> list[T]:
        released: list[T] = []
        while self.last + 1 in self._pending:
            self.last += 1
            item = self._pending.pop(self.last)
            if item is not None:
                released.append(item)
        return released
