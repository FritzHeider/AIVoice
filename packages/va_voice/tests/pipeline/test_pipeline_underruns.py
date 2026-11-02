"""CallPipeline reports platform underruns of its outbound clock (specs/07 §9 Decision P05-T17e)."""

from __future__ import annotations

from pipeline_fakes import ScriptedBrain, hanging
from pipeline_harness import harness
from service_helpers import json_log_stream, log_events

from va_voice.pipeline import SpeakText

LONG = (
    "This is a fairly long sentence that keeps the agent speaking for a while, so the caller can hear it, "
    "and here is a second clause, which keeps the audio going for another few hundred milliseconds. "
)
STALLED_FRAME = 20
STALL_S = 0.2


def _ended(stream: object) -> dict[str, object]:
    events = [e for e in log_events(stream) if e["event"] == "pipeline_ended"]  # type: ignore[arg-type]
    assert len(events) == 1
    return events[0]


async def test_a_stalled_send_mid_utterance_reports_one_underrun() -> None:
    stream = json_log_stream()
    underruns: list[float] = []
    brain = ScriptedBrain(opener=[SpeakText(hanging(LONG))])
    async with harness(brain=brain, on_underrun=lambda: underruns.append(h.now_ms())) as h:
        record = h.transport.send_audio
        sent = 0

        async def stalling_send(frame: bytes) -> None:
            nonlocal sent
            sent += 1
            if sent == STALLED_FRAME:
                await h.clock.sleep(STALL_S)
            await record(frame)

        h.transport.send_audio = stalling_send  # type: ignore[method-assign]
        await h.until(lambda: len(h.transport.frames) > STALLED_FRAME + 10)
        h.transport.close()
        await h.result()
    assert len(underruns) == 1
    ended = _ended(stream)
    assert ended["underruns"] == 1
    assert isinstance(ended["outbound_frames"], int)
    assert ended["outbound_frames"] > STALLED_FRAME
    assert ended["late_frames"] == 1
    assert isinstance(ended["max_lateness_ms"], float)
    assert ended["max_lateness_ms"] >= STALL_S * 1000 - 60


async def test_on_time_playback_reports_no_underrun() -> None:
    stream = json_log_stream()
    underruns: list[float] = []
    brain = ScriptedBrain(opener=[SpeakText(hanging(LONG))])
    async with harness(brain=brain, on_underrun=lambda: underruns.append(0.0)) as h:
        await h.until(lambda: len(h.transport.frames) > STALLED_FRAME + 10)
        h.transport.close()
        await h.result()
    assert underruns == []
    ended = _ended(stream)
    assert (ended["underruns"], ended["late_frames"]) == (0, 0)
