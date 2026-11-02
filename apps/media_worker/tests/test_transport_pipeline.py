"""``CallPipeline`` over ``TelnyxMediaTransport``: the opener goes out as Telnyx ``media`` + ``mark``
messages, the mark echo finishes the utterance, and ``stop`` ends the pipeline with ``transport_closed``."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import orjson
from media_helpers import FakeSocket, mark_msg, media_msg, stop_msg
from pipeline_fakes import EnergyVad, FakeStt, FakeTts, RecordingObserver, ScriptedBrain

from va_media.session import StreamStart
from va_media.transport import TelnyxMediaTransport
from va_sim.clock import AcceleratedClock
from va_telnyx.media import MediaFormat
from va_voice.audio.codec import ULAW_SILENCE
from va_voice.pipeline import CallPipeline, PipelineConfig
from va_voice.pipeline.models import SpeakStatic

START = StreamStart("stream-1", "cc-1", MediaFormat(encoding="PCMU", sample_rate=8000, channels=1))


async def until(clock: AcceleratedClock, predicate: Callable[[], bool], timeout_s: float = 30.0) -> None:
    end = clock.monotonic() + timeout_s
    while not predicate():
        assert clock.monotonic() < end, "condition not met in virtual time"
        await clock.sleep(0.02)


def events_of(socket: FakeSocket) -> list[dict[str, object]]:
    return [orjson.loads(message) for message in socket.sent]


async def test_opener_audio_marks_and_stop() -> None:
    clock = AcceleratedClock()
    socket = FakeSocket()
    transport = TelnyxMediaTransport(socket, START)
    calls: list[tuple[str, float]] = []
    stt, tts = FakeStt(clock), FakeTts(clock, calls)
    brain = ScriptedBrain(opener=[SpeakStatic("Hello, this is the agent.")])
    brain.bind(clock, calls)
    observer = RecordingObserver()
    pipeline = CallPipeline(
        transport=transport,
        stt=stt,
        tts=tts,
        vad=EnergyVad(),
        brain=brain,
        clock=clock,
        config=PipelineConfig(no_input_timeout_ms=30_000),
        observer=observer,
    )
    task = asyncio.get_running_loop().create_task(pipeline.run())
    try:
        for chunk in range(1, 6):
            socket.feed(media_msg(chunk, bytes([ULAW_SILENCE]) * 160))
        await until(clock, lambda: any(event["event"] == "mark" for event in events_of(socket)))
        sent = events_of(socket)
        kinds = [event["event"] for event in sent]
        assert kinds[0] == "media"
        assert kinds[-1] == "mark"
        assert set(kinds[:-1]) == {"media"}
        mark = sent[-1]["mark"]
        assert isinstance(mark, dict)
        assert mark["name"] == "seg:0:0"
        assert observer.agent_turns == []

        socket.feed(mark_msg("seg:0:0"))
        await until(clock, lambda: bool(observer.agent_turns))
        assert observer.agent_turns[0].text.strip() == "Hello, this is the agent."
        assert transport.stats.marks == 1
        assert transport.stats.sent_frames == kinds.count("media")

        socket.feed(stop_msg())
        result = await task
    finally:
        if not task.done():
            task.cancel()
        for fake in (stt, tts):
            await fake.aclose()
    assert result.end_reason == "transport_closed"
    assert transport.stop_received
    assert transport.stats.frames == 5
