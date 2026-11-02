"""OutboundAudio: paced sends on a manual clock, marks, idle bed, clear, backoff (specs/07 §9, §10.2)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import numpy as np
import pytest
import pytest_asyncio
from clock_helpers import ManualClock, settle

from va_voice.audio import PCM16LE_16000, ULAW_8000, ulaw_encode
from va_voice.background import BackgroundLoop
from va_voice.outbound import FrameMixer, FramePacer, OutboundAudio
from va_voice.playback import PlaybackTracker

FRAME = 0.02


@dataclass
class RecordingSink:
    """Records ``(t_ms, kind, value)``; ``hold`` (when set) blocks the next ``send_audio`` until released."""

    clock: ManualClock
    sent: list[tuple[float, str, bytes | str]] = field(default_factory=list)
    hold: asyncio.Event | None = None
    holding: asyncio.Event = field(default_factory=asyncio.Event)

    async def send_audio(self, frame: bytes) -> None:
        if self.hold is not None:
            self.holding.set()
            await self.hold.wait()
            self.hold = None
        self.sent.append((self._now(), "audio", frame))

    async def send_mark(self, name: str) -> None:
        self.sent.append((self._now(), "mark", name))

    def audio_times(self) -> list[float]:
        return [t for t, kind, _ in self.sent if kind == "audio"]

    def kinds(self) -> list[str]:
        return [kind for _, kind, _ in self.sent]

    def marks(self) -> list[tuple[float, str | bytes]]:
        return [(t, value) for t, kind, value in self.sent if kind == "mark"]

    def _now(self) -> float:
        return round(self.clock.monotonic() * 1000, 6)


@dataclass
class Rig:
    clock: ManualClock
    sink: RecordingSink
    out: OutboundAudio


def _speech(n: int, value: int = 0x10) -> bytes:
    return bytes([value]) * (160 * n)


async def _rig(mixer: FrameMixer) -> AsyncIterator[Rig]:
    clock = ManualClock()
    sink = RecordingSink(clock)
    out = OutboundAudio(sink, mixer, clock=clock)
    task = asyncio.create_task(out.run())
    await settle()
    try:
        yield Rig(clock, sink, out)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest_asyncio.fixture
async def rig() -> AsyncIterator[Rig]:
    async for value in _rig(FrameMixer(ULAW_8000)):
        yield value


@pytest_asyncio.fixture
async def bg_rig() -> AsyncIterator[Rig]:
    bed = np.full(1600, 1000, dtype=np.int16)
    async for value in _rig(FrameMixer(ULAW_8000, background=BackgroundLoop(bed, 160))):
        yield value


async def test_an_utterance_is_paced_with_a_lead_and_a_mark_after_its_last_frame(rig: Rig) -> None:
    tracker = PlaybackTracker(1)
    rig.out.start_utterance(tracker)
    opened = rig.out.tracker
    rig.out.push_audio(_speech(6))
    rig.out.end_segment(12)
    rig.out.push_audio(_speech(4))
    rig.out.end_segment(8)
    rig.out.finish_utterance()
    assert (opened, rig.out.tracker) == (tracker, None)
    await settle()
    assert rig.sink.audio_times() == [0.0] * 4
    assert rig.out.buffered_frames == 6
    assert rig.out.buffered_ms == 120
    await rig.clock.wait_for_sleep(FRAME)
    await rig.clock.run_for(0.2)
    assert rig.sink.audio_times() == [0.0] * 4 + [20.0, 40.0, 60.0, 80.0, 100.0, 120.0]
    assert rig.sink.kinds() == ["audio"] * 6 + ["mark"] + ["audio"] * 4 + ["mark"]
    assert rig.sink.marks() == [(40.0, "seg:1:0"), (120.0, "seg:1:1")]
    assert [(s.start_ms, s.end_ms, s.chars) for s in tracker.segments] == [(0, 120, 12), (120, 200, 8)]
    assert tracker.finished
    assert tracker.t_first_frame_sent == 0.0
    stats = rig.out.stats
    assert (stats.frames_sent, stats.speech_frames, stats.idle_frames, stats.marks_sent) == (10, 10, 0, 2)
    assert (stats.late_frames, stats.dropped_frames, stats.underruns) == (0, 0, 0)
    assert stats.max_lateness_ms == pytest.approx(0.0, abs=1e-6)
    # Without background nothing is sent while idle and nobody sleeps.
    await rig.clock.run_for(1.0)
    assert len(rig.sink.sent) == 12
    assert rig.clock.sleepers == 0


async def test_chunks_are_reframed_and_the_tail_is_padded(rig: Rig) -> None:
    tracker = PlaybackTracker(2)
    rig.out.start_utterance(tracker)
    rig.out.push_audio(bytes([0x20]) * 100)
    assert rig.out.buffered_frames == 0
    rig.out.push_audio(bytes([0x20]) * 150)
    rig.out.finish_utterance()
    await rig.clock.run_for(0.1)
    frames = [value for _, kind, value in rig.sink.sent if kind == "audio"]
    assert frames == [bytes([0x20]) * 160, bytes([0x20]) * 90 + bytes([0xFF]) * 70]
    assert rig.sink.marks() == [(0.0, "seg:2:0")]
    assert tracker.segments[0].chars == 0
    assert tracker.finished


async def test_finish_utterance_adds_characters_to_the_trailing_segment(rig: Rig) -> None:
    tracker = PlaybackTracker(5)
    rig.out.start_utterance(tracker)
    rig.out.push_audio(bytes([0x20]) * 250)
    rig.out.finish_utterance(17)
    await rig.clock.run_for(0.1)
    assert [(s.end_ms, s.chars) for s in tracker.segments] == [(40, 17)]
    assert rig.sink.kinds() == ["audio", "audio", "mark"]


async def test_on_finished_reports_each_finished_tracker() -> None:
    clock = ManualClock()
    finished: list[PlaybackTracker] = []
    out = OutboundAudio(RecordingSink(clock), FrameMixer(ULAW_8000), clock=clock, on_finished=finished.append)
    task = asyncio.create_task(out.run())
    try:
        spoken, empty, stopped = PlaybackTracker(1), PlaybackTracker(2), PlaybackTracker(3)
        out.start_utterance(spoken)
        out.push_audio(_speech(2))
        out.finish_utterance(4)
        out.start_utterance(empty)
        out.finish_utterance()
        out.start_utterance(stopped)
        out.push_audio(_speech(8))
        out.finish_utterance()
        stopped.stop(0.0)
        await settle()
        await clock.run_for(0.4)
        assert finished == [spoken, empty]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_characters_of_an_empty_segment_carry_forward(rig: Rig) -> None:
    tracker = PlaybackTracker(3)
    rig.out.start_utterance(tracker)
    rig.out.push_audio(bytes(80))
    rig.out.end_segment(5)
    rig.out.push_audio(bytes(240))
    rig.out.end_segment(7)
    rig.out.finish_utterance()
    await rig.clock.run_for(0.1)
    assert [(s.end_ms, s.chars) for s in tracker.segments] == [(40, 12)]
    assert rig.sink.kinds() == ["audio", "audio", "mark"]


async def test_an_empty_utterance_just_finishes(rig: Rig) -> None:
    tracker = PlaybackTracker(4)
    rig.out.start_utterance(tracker)
    rig.out.finish_utterance()
    await settle()
    assert tracker.finished
    assert rig.sink.sent == []


async def test_speech_after_idle_starts_with_a_new_burst(rig: Rig) -> None:
    first = PlaybackTracker(1)
    rig.out.start_utterance(first)
    rig.out.push_audio(_speech(2))
    rig.out.finish_utterance()
    await rig.clock.run_for(1.0)
    second = PlaybackTracker(2)
    rig.out.start_utterance(second)
    rig.out.push_audio(_speech(5))
    rig.out.finish_utterance()
    await rig.clock.run_for(0.1)
    assert rig.sink.audio_times() == [0.0, 0.0] + [1000.0] * 4 + [1020.0]
    assert rig.out.stats.late_frames == 0
    assert second.t_first_frame_sent == 1000.0


async def test_queued_utterances_keep_their_own_trackers(rig: Rig) -> None:
    first, second = PlaybackTracker(1), PlaybackTracker(2)
    rig.out.start_utterance(first)
    rig.out.push_audio(_speech(5))
    rig.out.finish_utterance()
    rig.out.start_utterance(second)
    rig.out.push_audio(_speech(2))
    rig.out.finish_utterance()
    await rig.clock.run_for(0.2)
    assert (first.frames_sent, second.frames_sent) == (5, 2)
    assert [value for _, value in rig.sink.marks()] == ["seg:1:0", "seg:2:0"]
    assert rig.sink.audio_times() == [0.0] * 4 + [20.0, 40.0, 60.0]


async def test_background_fills_idle_time_and_mixes_under_speech(bg_rig: Rig) -> None:
    await bg_rig.clock.run_for(0.1)
    idle = ulaw_encode(np.full(160, 1000, dtype=np.int16))
    assert bg_rig.sink.audio_times() == [0.0] * 4 + [20.0, 40.0, 60.0, 80.0, 100.0]
    assert {value for _, _, value in bg_rig.sink.sent} == {idle}
    tracker = PlaybackTracker(1)
    bg_rig.out.start_utterance(tracker)
    bg_rig.out.push_audio(bytes([0xFF]) * 320)
    bg_rig.out.finish_utterance()
    await bg_rig.clock.run_for(0.1)
    after = bg_rig.sink.sent[9:]
    assert [t for t, kind, _ in after if kind == "audio"] == [120.0, 140.0, 160.0, 180.0, 200.0]
    assert [kind for _, kind, _ in after][:3] == ["audio", "audio", "mark"]
    assert {value for _, kind, value in after if kind == "audio"} == {idle}
    stats = bg_rig.out.stats
    assert (stats.speech_frames, stats.idle_frames) == (2, 12)
    assert tracker.finished


async def test_clear_drops_queued_audio_and_resets_the_lead(rig: Rig) -> None:
    tracker = PlaybackTracker(1)
    rig.out.start_utterance(tracker)
    rig.out.push_audio(_speech(20))
    rig.out.end_segment(30)
    rig.out.push_audio(bytes(40))
    await rig.clock.wait_for_sleep(FRAME)
    await rig.clock.run_for(0.04)
    assert len(rig.sink.audio_times()) == 6
    tracker.stop(45.0)
    assert rig.out.clear() == 14
    assert rig.out.tracker is None
    assert rig.out.stats.dropped_frames == 14
    assert rig.out.pacer.anchor_ms is None
    await rig.clock.run_for(0.2)
    assert len(rig.sink.audio_times()) == 6
    assert rig.sink.marks() == []
    nxt = PlaybackTracker(2)
    rig.out.start_utterance(nxt)
    rig.out.push_audio(_speech(5))
    rig.out.finish_utterance()
    await rig.clock.run_for(0.1)
    assert rig.sink.audio_times()[6:] == [240.0] * 4 + [260.0]
    assert nxt.frames_sent == 5
    assert tracker.frames_sent == 6


async def test_a_clear_while_a_frame_is_in_flight_ignores_its_completion(rig: Rig) -> None:
    hold = asyncio.Event()
    rig.sink.hold = hold
    tracker = PlaybackTracker(1)
    rig.out.start_utterance(tracker)
    rig.out.push_audio(_speech(3))
    rig.out.end_segment(4)
    await rig.sink.holding.wait()
    rig.out.clear()
    hold.set()
    await settle()
    assert rig.sink.kinds() == ["audio"]
    assert tracker.frames_sent == 0
    assert rig.out.pacer.anchor_ms is None


async def test_a_mark_after_a_stopped_tracker_is_not_sent(rig: Rig) -> None:
    tracker = PlaybackTracker(1)
    rig.out.start_utterance(tracker)
    rig.out.push_audio(_speech(1))
    rig.out.end_segment(3)
    tracker.stop(0.0)
    rig.out.finish_utterance()
    await rig.clock.run_for(0.05)
    assert rig.sink.kinds() == ["audio"]
    assert not tracker.finished


async def test_rate_limit_pauses_sends_and_reduces_the_lead(rig: Rig) -> None:
    tracker = PlaybackTracker(1)
    rig.out.start_utterance(tracker)
    rig.out.push_audio(_speech(12))
    rig.out.finish_utterance()
    await rig.clock.wait_for_sleep(FRAME)
    await rig.clock.run_for(0.02)
    rig.out.rate_limited()
    await rig.clock.run_for(0.3)
    assert rig.sink.audio_times() == [0.0] * 4 + [20.0, 120.0, 120.0, 140.0, 160.0, 180.0, 200.0, 220.0]
    assert rig.out.stats.late_frames == 0
    # The pause outlasts the 60 ms lead: the caller hears a hole mid-utterance.
    assert rig.out.stats.underruns == 1


async def test_a_late_loop_counts_late_frames() -> None:
    clock = ManualClock()
    sink = RecordingSink(clock)
    out = OutboundAudio(sink, FrameMixer(ULAW_8000), clock=clock, pacer=FramePacer(lead_frames=0))
    tracker = PlaybackTracker(1)
    out.start_utterance(tracker)
    out.push_audio(_speech(2))
    out.finish_utterance()
    task = asyncio.create_task(out.run())
    await settle()
    await clock.wait_for_sleep(FRAME)
    clock.advance(0.06)
    await clock.run_for(0.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sink.audio_times() == [0.0, 60.0]
    assert out.stats.late_frames == 1
    assert out.stats.underruns == 1
    assert out.stats.max_lateness_ms == pytest.approx(40.0)


async def test_hd_frames_are_paced_in_the_wire_byte_order() -> None:
    clock = ManualClock()
    sink = RecordingSink(clock)
    out = OutboundAudio(sink, FrameMixer(PCM16LE_16000), clock=clock)
    out.start_utterance(PlaybackTracker(1, PCM16LE_16000))
    out.push_audio(b"\x01\x02" * 320)
    out.finish_utterance()
    task = asyncio.create_task(out.run())
    await settle()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [value for _, kind, value in sink.sent if kind == "audio"] == [b"\x02\x01" * 320]


async def test_utterance_misuse_is_rejected(rig: Rig) -> None:
    with pytest.raises(RuntimeError, match="no open utterance"):
        rig.out.push_audio(_speech(1))
    with pytest.raises(RuntimeError, match="no open utterance"):
        rig.out.end_segment(1)
    with pytest.raises(RuntimeError, match="no open utterance"):
        rig.out.finish_utterance()
    rig.out.start_utterance(PlaybackTracker(1))
    with pytest.raises(RuntimeError, match="still open"):
        rig.out.start_utterance(PlaybackTracker(2))


@dataclass
class UnderrunRig:
    clock: ManualClock
    sink: RecordingSink
    out: OutboundAudio
    underruns: list[float]


async def _underrun_rig(mixer: FrameMixer) -> AsyncIterator[UnderrunRig]:
    clock = ManualClock()
    sink = RecordingSink(clock)
    underruns: list[float] = []
    out = OutboundAudio(sink, mixer, clock=clock, on_underrun=lambda: underruns.append(clock.monotonic()))
    task = asyncio.create_task(out.run())
    await settle()
    try:
        yield UnderrunRig(clock, sink, out, underruns)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest_asyncio.fixture
async def urig() -> AsyncIterator[UnderrunRig]:
    async for value in _underrun_rig(FrameMixer(ULAW_8000)):
        yield value


@pytest_asyncio.fixture
async def bg_urig() -> AsyncIterator[UnderrunRig]:
    bed = np.full(1600, 1000, dtype=np.int16)
    async for value in _underrun_rig(FrameMixer(ULAW_8000, background=BackgroundLoop(bed, 160))):
        yield value


async def test_on_time_speech_counts_no_underrun(urig: UnderrunRig) -> None:
    urig.out.start_utterance(PlaybackTracker(1))
    urig.out.push_audio(_speech(20))
    urig.out.finish_utterance()
    await urig.clock.wait_for_sleep(FRAME)
    await urig.clock.run_for(0.5)
    assert len(urig.sink.audio_times()) == 20
    assert (urig.out.stats.underruns, urig.underruns) == (0, [])


async def test_a_stalled_send_loop_mid_utterance_counts_one_underrun(urig: UnderrunRig) -> None:
    tracker = PlaybackTracker(1)
    urig.out.start_utterance(tracker)
    urig.out.push_audio(_speech(20))
    urig.out.finish_utterance()
    await urig.clock.wait_for_sleep(FRAME)
    await urig.clock.run_for(0.04)
    hold = asyncio.Event()
    urig.sink.hold = hold
    urig.sink.holding.clear()
    await urig.clock.run_for(0.02)
    await urig.sink.holding.wait()
    urig.clock.advance(0.2)
    hold.set()
    await settle()
    await urig.clock.run_for(0.5)
    assert tracker.frames_sent == 20
    assert urig.out.stats.underruns == 1
    assert len(urig.underruns) == 1


async def test_tts_starvation_without_a_background_is_not_an_underrun(urig: UnderrunRig) -> None:
    tracker = PlaybackTracker(1)
    urig.out.start_utterance(tracker)
    urig.out.push_audio(_speech(5))
    await urig.clock.wait_for_sleep(FRAME)
    await urig.clock.run_for(0.3)
    urig.out.push_audio(_speech(5))
    urig.out.finish_utterance()
    await settle()
    await urig.clock.run_for(0.3)
    assert tracker.frames_sent == 10
    assert (urig.out.stats.underruns, urig.underruns) == (0, [])


async def test_tts_starvation_under_a_background_is_not_an_underrun(bg_urig: UnderrunRig) -> None:
    tracker = PlaybackTracker(1)
    bg_urig.out.start_utterance(tracker)
    bg_urig.out.push_audio(_speech(5))
    await bg_urig.clock.run_for(0.3)
    bg_urig.out.push_audio(_speech(5))
    bg_urig.out.finish_utterance()
    await bg_urig.clock.run_for(0.3)
    assert tracker.frames_sent == 10
    assert bg_urig.out.stats.idle_frames > 0
    assert (bg_urig.out.stats.underruns, bg_urig.underruns) == (0, [])


async def test_a_clear_resets_the_continuity(urig: UnderrunRig) -> None:
    tracker = PlaybackTracker(1)
    urig.out.start_utterance(tracker)
    urig.out.push_audio(_speech(20))
    await urig.clock.wait_for_sleep(FRAME)
    await urig.clock.run_for(0.04)
    urig.out.clear()
    await urig.clock.run_for(0.2)
    urig.out.start_utterance(tracker)
    urig.out.push_audio(_speech(5))
    urig.out.finish_utterance()
    await settle()
    await urig.clock.run_for(0.2)
    assert tracker.frames_sent == 11
    assert (urig.out.stats.underruns, urig.underruns) == (0, [])
