"""CallPipeline turn loop: opener, user turn → Brain → reply audio with marks, latency (Decision P04-T12a)."""

from __future__ import annotations

import itertools

import numpy as np
import pytest
from pipeline_fakes import FakeTtsChunk, Said, ScriptedBrain, tokens
from pipeline_harness import harness

from va_common.time import Clock
from va_voice.audio import L16_16000, PCM16LE_16000, ULAW_8000, AudioFormat
from va_voice.latency import LatencyTracer
from va_voice.pipeline import PipelineConfig, SpeakStatic, SpeakText
from va_voice.playback import mark_name
from va_voice.text.pronunciation import PronunciationRule

REPLY = "Sure, I can help with that. What is your order number?"


def speech_frames(frames: list[tuple[float, bytes]], silence: int) -> list[tuple[float, bytes]]:
    """Frames carrying agent speech (the fakes' TTS bytes, not silence)."""
    return [(t, frame) for t, frame in frames if frame[0] != silence]


async def test_opener_is_spoken_with_a_mark_and_recorded() -> None:
    brain = ScriptedBrain(opener=[SpeakStatic("Hi, this is Ava.")])
    async with harness(brain=brain) as h:
        await h.until(lambda: len(h.observer.agent_turns) == 1)
        turn = h.observer.agent_turns[0]
        assert (turn.index, turn.text, turn.text_spoken, turn.interrupted) == (
            0,
            "Hi, this is Ava.",
            turn.text,
            False,
        )
        assert h.tts.started == ["turn-0"]
        assert h.tts.sent == [("turn-0", "Hi, this is Ava. ", True)]
        assert h.tts.flushed == ["turn-0"]
        assert [name for name, _ in h.transport.calls if name.startswith("tts.")] == [
            "tts.start_context",
            "tts.send",
            "tts.flush",
        ]
        assert [name for _, name in h.transport.marks] == [mark_name(0, 0)]
        echo_ms = h.transport.echoes[0][0]
        assert turn.ended_ms >= echo_ms
        # 17 chars x 5 ms = 85 ms of μ-law audio -> 5 frames (the last one padded).
        assert len(h.transport.frames) == 5
        assert turn.started_ms == h.transport.frames[0][0]
        assert h.observer.reports == []
        assert h.brain.closed == 1


async def test_normal_turn_commits_the_stt_and_speaks_the_reply() -> None:
    brain = ScriptedBrain(replies=[[SpeakText(tokens(REPLY))]])
    async with harness(
        brain=brain, utterances=[Said("I need help with my order.", partial="I need help")]
    ) as h:
        await h.turn(600)
        await h.until(lambda: len(h.observer.agent_turns) == 1)
        user = h.brain.user_turns[0]
        assert user.text == "I need help with my order."
        assert user.words == ("I", "need", "help", "with", "my", "order.")
        assert user.language_code == "en"
        assert user.index == 0
        assert h.observer.user_turns == [user]
        assert user.end_ms - user.start_ms == pytest.approx(600, abs=40)
        assert len(h.stt.commits) == 1
        assert h.stt.commits[0] > user.end_ms
        agent = h.observer.agent_turns[0]
        assert agent.index == 1
        assert agent.text == REPLY
        assert "".join(text for _, text, _ in h.tts.sent) == f"{REPLY} "
        assert all(flush for _, _, flush in h.tts.sent)
        frames = [t for t, _ in h.transport.frames]
        # A burst of lead + 1 frames, then one frame every 20 ms.
        assert frames[:4] == [frames[0]] * 4
        steps = {round(b - a, 3) for a, b in itertools.pairwise(frames[3:])}
        assert steps == {20.0}
        assert agent.ended_ms >= h.transport.echoes[-1][0]
        assert h.pipeline.stats.user_turns == 1
        assert h.pipeline.stats.agent_turns == 1


async def test_latency_report_measures_the_turn() -> None:
    brain = ScriptedBrain(replies=[[SpeakText(tokens(REPLY))]])
    async with harness(brain=brain, utterances=["Hello there."]) as h:
        await h.turn(600)
        await h.until(lambda: len(h.observer.reports) == 1)
        latency = h.observer.reports[0].latency
        assert latency.eot_ms is not None
        assert latency.e2e_ms is not None
        assert latency.stt_final_ms == 0
        assert latency.tts_ttfa_ms == 0
        assert latency.llm_ttft_ms == 0
        # Zero-latency fakes: everything after the end-of-turn decision is platform overhead.
        assert latency.e2e_ms - latency.eot_ms <= 30
        events = h.observer.reports[0].events
        assert events[0].message == f"E2E: {latency.e2e_ms}ms"
        assert events[0].data["turn_index"] == 0
        assert [event.message.split(":")[0] for event in events[1:]] == ["STT", "LLM", "TTS"]


async def test_vendor_latencies_appear_in_the_report() -> None:
    def tracer(clock: Clock) -> LatencyTracer:
        return LatencyTracer(clock)

    async with harness(
        brain=ScriptedBrain(replies=[[]]),
        utterances=["Hello there."],
        ttfa_s=0.15,
        commit_latency_s=0.1,
        latency=tracer,
    ) as h:
        h.brain.replies[0].append(SpeakText(tokens(REPLY, h.clock, first_delay_s=0.2)))
        await h.turn(600)
        await h.until(lambda: len(h.observer.reports) == 1)
        latency = h.observer.reports[0].latency
        assert (latency.stt_final_ms, latency.llm_ttft_ms, latency.tts_ttfa_ms) == (100, 200, 150)
        assert latency.e2e_ms is not None
        assert latency.eot_ms is not None
        assert latency.e2e_ms - latency.eot_ms == pytest.approx(450, abs=2)


async def test_brain_supplied_llm_request_time_is_used() -> None:
    async with harness(brain=ScriptedBrain(replies=[[]]), utterances=["Hello there."]) as h:
        output = SpeakText(tokens(REPLY), llm_request_ms=h.now_ms())
        h.brain.replies[0].append(output)
        await h.turn(600)
        await h.until(lambda: len(h.observer.reports) == 1)
        llm = h.observer.reports[0].latency.llm_ttft_ms
        assert llm is not None
        assert llm > 600


async def test_commit_timeout_falls_back_to_the_partial() -> None:
    brain = ScriptedBrain(replies=[[SpeakStatic("Okay.")]])
    said = Said("I need help with my order.", partial="I need help with")
    async with harness(brain=brain, utterances=[said], commit_latency_s=2.0) as h:
        await h.turn(600)
        assert h.brain.user_turns[0].text == "I need help with"
        assert h.brain.user_turns[0].words == ()
        assert h.pipeline.stats.commit_timeouts == 1
        assert h.brain.user_turns[0].index == 0
        await h.until(lambda: len(h.observer.reports) == 1)
        assert h.observer.reports[0].latency.stt_final_ms == 1500
        await h.until(lambda: len(h.observer.agent_turns) == 1)


async def test_blank_committed_transcript_falls_back_to_the_partial() -> None:
    brain = ScriptedBrain(replies=[[]])
    async with harness(brain=brain, utterances=[Said("  ", partial="Hello")]) as h:
        await h.turn(600)
        assert h.brain.user_turns[0].text == "Hello"


async def test_pronunciation_changes_tts_text_but_not_the_transcript() -> None:
    rule = PronunciationRule("SQL", "sequel")
    config = PipelineConfig(pronunciation=(rule,))
    brain = ScriptedBrain(opener=[SpeakStatic("We use SQL daily.")])
    async with harness(brain=brain, config=config) as h:
        await h.until(lambda: len(h.observer.agent_turns) == 1)
        assert h.tts.sent == [("turn-0", "We use sequel daily. ", True)]
        assert h.observer.agent_turns[0].text == "We use SQL daily."


async def test_stale_tts_chunks_are_dropped() -> None:
    brain = ScriptedBrain(opener=[SpeakText(tokens("Hello there, how are you today?"))])
    async with harness(brain=brain, echo_delay_ms=None) as h:
        h.tts.emit(FakeTtsChunk("turn-99", b"\x55" * 160))
        await h.until(lambda: len(h.observer.agent_turns) == 1)
        sent = len(h.transport.frames)
        h.tts.emit(FakeTtsChunk("turn-0", b"\x55" * 160))
        h.tts.emit(FakeTtsChunk("turn-0", b"", is_final=True))
        await h.until(lambda: h.pipeline.stats.stale_tts_chunks == 3)
        await h.until(lambda: h.now_ms() >= 1000)
        assert len(h.transport.frames) == sent


async def test_noise_without_text_is_discarded() -> None:
    async with harness() as h:
        await h.caller.say(200)
        await h.until(lambda: h.pipeline.stats.discarded_turns == 1)
        assert h.brain.user_turns == []
        assert h.stt.commits == []


async def test_empty_speech_output_opens_no_tts_context() -> None:
    brain = ScriptedBrain(opener=[SpeakStatic("   "), SpeakText(tokens("")), SpeakStatic("Hi.")])
    async with harness(brain=brain) as h:
        await h.until(lambda: len(h.observer.agent_turns) == 1)
        assert h.tts.started == ["turn-0"]


async def test_reordered_inbound_frames_reach_the_stt_in_order() -> None:
    async with harness(streaming=False) as h:
        frames = [h.caller.frame(loud=False) for _ in range(4)]
        for chunk in (1, 3, 2, 4):
            h.caller.send(frames[chunk - 1], chunk)
        await h.until(lambda: len(h.stt.audio) == 4)
        assert h.stt.audio == frames
        assert h.pipeline.inbound.reorder.stats.reordered == 1


async def test_transport_close_drains_held_frames() -> None:
    async with harness(streaming=False) as h:
        frames = [h.caller.frame(loud=False) for _ in range(3)]
        h.caller.send(frames[0], 1)
        h.caller.send(frames[2], 3)
        h.transport.close()
        result = await h.result()
        assert result.end_reason == "transport_closed"
        assert h.stt.audio[0] == frames[0]
        assert h.stt.audio[-1] == frames[2]
        assert len(h.stt.audio) == 3


@pytest.mark.parametrize(
    ("fmt", "byte_order"),
    [(L16_16000, "big"), (L16_16000, "little"), (PCM16LE_16000, "big")],
)
async def test_16khz_legs_feed_the_vad_without_resampling(fmt: AudioFormat, byte_order: str) -> None:
    config = PipelineConfig(fmt=fmt, l16_byte_order="little" if byte_order == "little" else "big")
    brain = ScriptedBrain(replies=[[SpeakStatic("Okay.")]])
    async with harness(config=config, brain=brain, utterances=["Hello there."]) as h:
        await h.turn(600)
        await h.until(lambda: len(h.observer.agent_turns) == 1)
        fed = np.concatenate(h.vad.fed)
        sent = np.concatenate(h.caller.sent_pcm[: len(h.vad.fed)])
        assert np.array_equal(fed, sent)
        stt = np.frombuffer(b"".join(h.stt.audio), dtype="<i2")
        assert np.array_equal(stt, np.concatenate(h.caller.sent_pcm[: len(h.stt.audio)]))
        # pcm_16000 TTS audio goes out as 640-byte wire frames.
        assert {len(frame) for _, frame in h.transport.frames} == {640}


async def test_ulaw_leg_resamples_for_the_vad() -> None:
    async with harness(config=PipelineConfig(fmt=ULAW_8000)) as h:
        await h.until(lambda: len(h.vad.fed) >= 10)
        assert sum(chunk.size for chunk in h.vad.fed) > 0
        assert all(len(audio) == 160 for audio in h.stt.audio)
