"""FramePacer: drift-free targets, lead control, re-anchoring and rate-limit backoff (specs/07 §9)."""

from __future__ import annotations

import pytest

from va_voice.outbound import (
    LEAD_FRAMES,
    RATE_LIMIT_PAUSE_MS,
    RATE_LIMIT_WINDOW_MS,
    THROTTLED_LEAD_FRAMES,
    FramePacer,
)


def _send_due(pacer: FramePacer, now: float) -> float:
    """Send at the pacer's target (never before ``now``); returns the send time."""
    at = max(now, pacer.next_send_ms(now))
    pacer.on_sent(at)
    return at


def test_lead_frames_go_out_at_once_then_every_20_ms() -> None:
    pacer = FramePacer()
    times = []
    now = 1_000.0
    for _ in range(8):
        now = _send_due(pacer, now)
        times.append(now)
    assert times == [1_000.0] * (LEAD_FRAMES + 1) + [1_020.0, 1_040.0, 1_060.0, 1_080.0]
    assert pacer.anchor_ms == 1_000.0
    assert pacer.sent_since_anchor == 8


def test_targets_do_not_drift_with_late_wakeups() -> None:
    pacer = FramePacer()
    jitter = [0.0, 3.0, 7.5, 1.0, 12.0]
    pacer.on_sent(0.0)
    for n in range(1, 1000):
        target = pacer.next_send_ms(0.0)
        assert target == max(0.0, (n - LEAD_FRAMES) * 20.0)
        lateness = pacer.on_sent(target + jitter[n % len(jitter)])
        assert lateness == jitter[n % len(jitter)]
    assert pacer.next_send_ms(0.0) == (1000 - LEAD_FRAMES) * 20.0


def test_a_drained_buffer_reanchors_without_counting_the_gap() -> None:
    pacer = FramePacer()
    for _ in range(5):
        pacer.on_sent(0.0)
    assert not pacer.drained(99.9)
    assert pacer.drained(100.0)
    pacer.mark_gap()
    assert pacer.next_send_ms(500.0) == 500.0
    assert pacer.on_sent(500.0) == 0.0
    assert pacer.anchor_ms == 500.0
    assert pacer.sent_since_anchor == 1


def test_a_stalled_loop_reports_lateness_and_reanchors_instead_of_bursting() -> None:
    pacer = FramePacer()
    for _ in range(LEAD_FRAMES + 1):
        pacer.on_sent(0.0)
    # Frame 4 was due at 20 ms; the loop wakes at 150 ms, after Telnyx ran dry at 80 ms.
    assert pacer.on_sent(150.0) == pytest.approx(130.0)
    assert pacer.anchor_ms == 150.0
    assert [pacer.next_send_ms(150.0)] * 3 == [150.0] * 3


def test_reset_sends_the_next_frame_at_once() -> None:
    pacer = FramePacer()
    for _ in range(LEAD_FRAMES + 1):
        pacer.on_sent(0.0)
    assert pacer.next_send_ms(5.0) == 20.0
    pacer.reset()
    assert pacer.anchor_ms is None
    assert pacer.next_send_ms(5.0) == 5.0
    assert pacer.on_sent(5.0) == 0.0
    assert pacer.anchor_ms == 5.0


def test_throttle_pauses_then_keeps_a_one_frame_lead_for_five_seconds() -> None:
    pacer = FramePacer()
    now = 0.0
    for _ in range(10):
        now = _send_due(pacer, now)
    assert now == 120.0
    pacer.throttle(now)
    assert pacer.lead_frames_at(now) == THROTTLED_LEAD_FRAMES
    assert pacer.next_send_ms(now) == now + RATE_LIMIT_PAUSE_MS
    now = _send_due(pacer, now)
    assert now == 220.0
    # Telnyx drained during the pause: the schedule restarts from the paused send with a lead of one frame.
    assert pacer.anchor_ms == 220.0
    assert _send_due(pacer, now) == 220.0
    assert _send_due(pacer, now) == 240.0
    assert pacer.lead_frames_at(RATE_LIMIT_WINDOW_MS + 119.0) == THROTTLED_LEAD_FRAMES
    assert pacer.lead_frames_at(RATE_LIMIT_WINDOW_MS + 120.0) == LEAD_FRAMES


def test_lateness_during_the_pause_is_measured_from_its_end() -> None:
    pacer = FramePacer()
    for _ in range(LEAD_FRAMES + 2):
        pacer.on_sent(0.0)
    pacer.throttle(10.0)
    assert pacer.on_sent(115.0) == pytest.approx(5.0)


def test_lead_frames_must_not_be_negative() -> None:
    with pytest.raises(ValueError, match="lead_frames must be >= 0"):
        FramePacer(lead_frames=-1)


def test_zero_lead_sends_in_real_time() -> None:
    pacer = FramePacer(lead_frames=0)
    now = 0.0
    times = []
    for _ in range(3):
        now = _send_due(pacer, now)
        times.append(now)
    assert times == [0.0, 20.0, 40.0]
