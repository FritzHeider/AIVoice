"""The virtual playout clock of agent audio and the caller frame queue."""

from __future__ import annotations

from va_sim.telnyx.media_format import MediaFormat
from va_sim.telnyx.playout import CallerQueue, Playout

PCMU = MediaFormat("PCMU", 8000)
SILENCE = b"\xff" * 160


def frame(value: int) -> bytes:
    return bytes([value]) * 160


def test_mark_is_due_when_the_audio_before_it_has_played() -> None:
    playout = Playout(PCMU)
    playout.push(frame(1) + frame(2) + frame(3))
    playout.mark("m1")
    assert playout.queued_ms == 60
    assert playout.tick() == (frame(1), [])
    assert playout.tick() == (frame(2), [])
    assert playout.tick() == (frame(3), ["m1"])
    assert playout.tick() == (SILENCE, [])
    assert playout.queued_ms == 0


def test_mark_on_an_empty_queue_is_due_at_the_next_tick() -> None:
    playout = Playout(PCMU)
    playout.mark("now")
    playout.push(frame(4))
    playout.mark("after")
    assert playout.tick() == (frame(4), ["now", "after"])


def test_partial_frame_is_padded_with_silence() -> None:
    playout = Playout(MediaFormat("L16", 16000))
    playout.push(b"\x01\x02" * 100)
    played, marks = playout.tick()
    assert played == b"\x01\x02" * 100 + bytes(440)
    assert marks == []


def test_clear_drops_audio_and_releases_pending_marks_in_order() -> None:
    playout = Playout(PCMU)
    playout.push(frame(1) * 50)
    playout.mark("a")
    playout.push(frame(2))
    playout.mark("b")
    assert playout.tick() == (frame(1), [])
    playout.clear()
    assert playout.tick() == (SILENCE, ["a", "b"])
    assert playout.tick() == (SILENCE, [])
    playout.push(frame(5))
    playout.mark("c")
    assert playout.tick() == (frame(5), ["c"])


def test_caller_queue_reports_utterance_start_and_end() -> None:
    queue = CallerQueue(PCMU)
    first: dict[str, object] = {"utterance_id": 1}
    second: dict[str, object] = {"utterance_id": 2}
    queue.push(frame(1) + frame(2)[:80], first)
    queue.push(frame(3), second)
    one = queue.next_frame()
    assert (one.payload, one.started, one.ended) == (frame(1), [first], [])
    two = queue.next_frame()
    assert (two.payload, two.started, two.ended) == (frame(2)[:80] + frame(3)[:80], [second], [first])
    three = queue.next_frame()
    assert (three.payload, three.started, three.ended) == (frame(3)[:80] + SILENCE[:80], [], [second])
    idle = queue.next_frame()
    assert (idle.payload, idle.started, idle.ended) == (SILENCE, [], [])
    queue.push(frame(9))
    assert queue.next_frame().payload == frame(9)
