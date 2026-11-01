"""Commit throttle window, FIFO answers, and the timestamp fallback (Decision P03-T11 rules 4 and 5)."""

from __future__ import annotations

from va_elevenlabs.stt_commits import Awaiting, CommitTracker, PendingCommit, Resolved


def test_first_commit_goes_out_now_and_later_ones_are_throttled() -> None:
    tracker = CommitTracker(0.25, 0.3)
    assert tracker.request(0.0) is None
    tracker.sent(0.0, 100, 0.0)
    assert tracker.request(0.1) == 0.25
    assert tracker.request(0.2) == 0.25
    assert (tracker.deferred, tracker.next_deadline()) == (0.25, 0.25)
    assert tracker.take_due(0.24) is None
    assert tracker.take_due(0.25) == 0.1
    assert (tracker.deferred, tracker.take_due(0.3)) == (None, None)
    tracker.sent(0.25, 200, 0.1)
    assert tracker.request(0.6) is None
    assert (tracker.outstanding, tracker.last_mark) == (2, 200)


def test_throttled_resends_one_interval_later() -> None:
    tracker = CommitTracker(0.25, 0.3)
    tracker.sent(1.0, 100, 0.9)
    assert tracker.throttled(1.05) == 1.3
    assert tracker.outstanding == 0
    assert tracker.take_due(1.3) == 0.9
    tracker.sent(1.3, 150, 0.9)
    assert tracker.request(1.4) == 1.55
    assert tracker.throttled(1.45) == 1.55
    assert tracker.take_due(1.55) == 0.9
    assert tracker.throttled(2.0) == 2.25
    assert tracker.take_due(2.25) == 2.0


def test_plain_then_words_resolve_in_order() -> None:
    tracker = CommitTracker(0.25, 0.3)
    tracker.sent(0.0, 100, 0.0)
    tracker.sent(0.3, 200, 0.3)
    tracker.plain("one", 0.5)
    assert tracker.next_deadline() == 0.8
    assert tracker.with_words() == Resolved(PendingCommit(0.0, 100))
    assert tracker.with_words() == Resolved(PendingCommit(0.3, 200))
    assert tracker.with_words() == Resolved(None)


def test_fallback_expiry_drops_the_late_variant() -> None:
    tracker = CommitTracker(0.25, 0.3)
    tracker.sent(0.0, 100, 0.0)
    tracker.plain("one", 0.5)
    assert tracker.expired(0.79) == []
    assert tracker.expired(0.8) == [Awaiting(PendingCommit(0.0, 100), "one", 0.8)]
    assert tracker.with_words() is None
    assert tracker.with_words() == Resolved(None)


def test_collapse_and_flush_after_a_reconnect() -> None:
    tracker = CommitTracker(0.25, 0.3)
    tracker.collapse(0.0)
    assert tracker.outstanding == 0
    tracker.sent(0.0, 100, 0.0)
    tracker.sent(0.3, 200, 0.2)
    tracker.sent(0.6, 300, 0.6)
    tracker.plain("one", 0.7)
    assert tracker.flush_awaiting() == [Awaiting(PendingCommit(0.0, 100), "one", 1.0)]
    tracker.collapse(1.0)
    assert (tracker.outstanding, tracker.last_mark) == (1, 300)
    assert tracker.request(1.1) == 1.25
    assert tracker.with_words() == Resolved(PendingCommit(0.2, 300))
    assert tracker.next_deadline() == 1.25
