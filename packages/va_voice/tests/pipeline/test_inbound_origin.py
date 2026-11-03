"""InboundPath's stream-time origin (specs/07-media-pipeline.md §16, Decision P05-T17e2)."""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from va_voice.audio import L16_16000, pcm_to_l16
from va_voice.pipeline import InboundAudio, InboundPath, PipelineConfig
from va_voice.vad import VadEvent

FRAME = L16_16000.frame_samples


class StreamTimeVad:
    """Reports one ``probability`` event at the stream time where each feed ends."""

    def __init__(self) -> None:
        self.samples = 0

    async def feed(self, pcm16k: NDArray[np.int16]) -> list[VadEvent]:
        self.samples += pcm16k.size
        return [VadEvent("probability", self.samples / 16, 0.0, speech=False)]


class NullStt:
    async def send_audio(self, audio: bytes) -> None:
        del audio


class Clock:
    def __init__(self, now_ms: float) -> None:
        self.now = now_ms

    def __call__(self) -> float:
        return self.now


def _frame(chunk: int) -> InboundAudio:
    return InboundAudio(chunk=chunk, payload=pcm_to_l16(np.full(FRAME, 100, dtype=np.int16), "big"))


def _path(clock: Clock) -> InboundPath:
    config = PipelineConfig(fmt=L16_16000)
    return InboundPath(config, stt=NullStt(), vad=StreamTimeVad(), now_ms=clock)  # type: ignore[arg-type]


async def test_on_time_frames_keep_the_first_frame_origin() -> None:
    clock = Clock(1000.0)
    path = _path(clock)
    events = []
    for chunk in range(1, 6):
        events.extend(await path.push(_frame(chunk)))
        clock.now += 20.0
    assert path.origin_ms == 980.0
    # Each frame's end maps to its arrival time.
    assert [e.at_ms for e in events] == [1000.0, 1020.0, 1040.0, 1060.0, 1080.0]


async def test_a_startup_backlog_does_not_delay_the_timeline() -> None:
    clock = Clock(1000.0)
    path = _path(clock)
    # Ten frames sent from t = 800 queued while the call attached and are all read at t = 1000 ...
    backlog = [e for chunk in range(1, 11) for e in await path.push(_frame(chunk))]
    assert path.origin_ms == 800.0
    assert backlog[-1].at_ms == 1000.0
    # ... then frames arrive in real time: frame 11 (stream end 220 ms) at t = 1020 maps to its arrival.
    clock.now = 1020.0
    [event] = await path.push(_frame(11))
    assert (path.origin_ms, event.at_ms) == (800.0, 1020.0)


async def test_late_frames_never_move_the_origin_later() -> None:
    clock = Clock(1000.0)
    path = _path(clock)
    await path.push(_frame(1))
    clock.now = 1200.0  # frame 2 is 180 ms late
    [late] = await path.push(_frame(2))
    assert path.origin_ms == 980.0
    assert late.at_ms == 1020.0
