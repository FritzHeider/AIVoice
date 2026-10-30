"""Scenario caller runner against a test agent: greeting (text and WAV), wait_agent_speech, say and barge-in,
DTMF (events and in-band), silence, expect_hangup, hangup, transfer targets, interruption, and reporting."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import httpx
import numpy as np
import pytest
from media_agent import MediaAgent
from numpy.typing import NDArray
from telnyx_helpers import (
    CALLEE,
    RecordingTransport,
    at,
    create_app,
    dial,
    inbound_call,
    ok,
    payloads,
    register,
    settle,
    summary,
    telnyx_client,
    telnyx_state,
)

from va_sim.audio import MAX_UTTERANCE_ID, dbfs_peak, decode_marker, goertzel_power, ulaw_encode
from va_sim.state import SimState, Utterance
from va_sim.telnyx.calls import Call
from va_sim.telnyx.media_format import MediaFormat
from va_sim.wav import pcm16_wav

PCMU: Final = MediaFormat("PCMU", 8000)
L16_WIDE: Final = MediaFormat("L16", 16000)
TRANSFER_TO: Final = "+14155550199"

type Script = dict[str, Any]


# Builders.


def scenario_text(
    steps: list[str] | None = None, *, greeting: str | None = None, inbound: bool = False, extra: str = ""
) -> str:
    """A scenario for calls to (or, inbound, from) :data:`CALLEE`; ``steps`` are YAML flow mappings."""
    binding = f'inbound_from: "{CALLEE}"' if inbound else f'to: "{CALLEE}"'
    text = f"id: scripted\nmatch: {{{binding}}}\n"
    if not inbound:
        text += "answer: {after_ms: 1000}\n"
    if greeting is not None:
        text += f"greeting_audio: {json.dumps(greeting)}\n"
    if steps:
        text += "steps:\n" + "".join(f"  - {step}\n" for step in steps)
    return text + extra


def loud(ms: int, freq: float = 440.0) -> bytes:
    """Agent speech stand-in: a μ-law sine at -12 dBFS."""
    t = np.arange(ms * 8) / 8000
    return ulaw_encode(np.rint(dbfs_peak(-12) * np.sin(2 * np.pi * freq * t)).astype(np.int16))


async def speak(agent: MediaAgent, audio: bytes) -> None:
    await agent.send({"event": "media", "media": {"payload": base64.b64encode(audio).decode()}})


def started(agent: MediaAgent) -> bool:
    return len(agent.frames) >= 2


async def scripted_call(
    client: httpx.AsyncClient, state: SimState, agent: MediaAgent, scenario: str, **dial_fields: Any
) -> str:
    """Register ``scenario``, dial :data:`CALLEE` streaming to ``agent``, wait for ``start``."""
    register(state, scenario)
    agent.clock = state.clock
    app = await create_app(client)
    ccid: str = (await dial(client, app["id"], stream_url=agent.url, **dial_fields))["call_control_id"]
    await at(state, 1.0)
    await agent.wait_for(started)
    return ccid


# Inspection.


def leg(state: SimState, ccid: str) -> Call:
    found = state.telnyx.call(ccid)
    assert found is not None
    return found[1]


def script_of(state: SimState, ccid: str) -> Script | None:
    return leg(state, ccid).script_summary()


async def until(state: SimState, predicate: Callable[[], bool], limit_s: float = 30.0) -> None:
    """Sleep on the sim clock in 20 ms steps until ``predicate()`` holds (at most ``limit_s`` virtual s)."""
    deadline = state.clock.virtual_ms() + limit_s * 1000
    while not predicate():
        assert state.clock.virtual_ms() < deadline, "condition not reached in time"
        await state.clock.sleep(0.02)


async def finished(state: SimState, ccid: str, limit_s: float = 30.0) -> Script:
    """Wait until the leg's script is no longer running; returns its summary."""

    def done() -> bool:
        script = script_of(state, ccid)
        return script is not None and script["status"] != "running"

    await until(state, done, limit_s)
    script = script_of(state, ccid)
    assert script is not None
    return script


def caller_pcm(agent: MediaAgent, fmt: MediaFormat) -> NDArray[np.int16]:
    """Every caller media payload so far, decoded at the stream rate."""
    wire = b"".join(base64.b64decode(frame["media"]["payload"]) for frame in agent.media())
    return fmt.to_pcm(wire)


def tone_power(pcm: NDArray[np.int16], freq_hz: float, sample_rate: int) -> float:
    return goertzel_power(pcm.astype(np.float64), freq_hz, sample_rate)


def agent_speech_frames(call: Call) -> list[int]:
    """Indexes of the 20 ms agent track frames that are speech (RMS above 256)."""
    track = np.frombuffer(bytes(call.audio.agent), dtype="<i2").astype(np.float64)
    frames = track[: track.size // 160 * 160].reshape(-1, 160)
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    return [int(index) for index in np.flatnonzero(rms > 256)]


def track_ms(call: Call, frame_index: int) -> int:
    """The sim time a 20 ms track frame was played (tracks are aligned with the clock from answer)."""
    assert call.answered_ms is not None
    return call.answered_ms + frame_index * 20


def tone_wav(path: Path, freq: float, ms: int, rate: int = 8000) -> Path:
    t = np.arange(ms * rate // 1000) / rate
    samples = np.rint(9000 * np.sin(2 * np.pi * freq * t)).astype("<i2")
    path.write_bytes(pcm16_wav([samples.tobytes()], rate))
    return path


def timestamp_s(at_iso: str) -> float:
    return datetime.fromisoformat(at_iso).timestamp()


# Greeting.


async def test_text_greeting_plays_first_without_losing_time(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        ccid = await scripted_call(client, state, media_agent, scenario_text(greeting="Hello?"))
        script = await finished(state, ccid)
        call = leg(state, ccid)
        greeting = script["greeting"]
        [entry] = call.timeline_utterances
        assert script["role"] == "caller"
        assert script["status"] == "done"
        assert script["steps"] == []
        assert greeting["source"] == "text"
        assert greeting["status"] == "ok"
        assert greeting["detail"] is None
        assert (entry["utterance_id"], entry["text"]) == (greeting["utterance_id"], "Hello?")
        assert greeting["started_ms"] == call.answered_ms
        assert entry["start_ms"] <= greeting["started_ms"] + 20
        assert greeting["ended_ms"] == entry["end_ms"]
        assert state.utterances[greeting["utterance_id"]].language == "en"
        assert decode_marker(caller_pcm(media_agent, PCMU), 8000) == greeting["utterance_id"]


@pytest.mark.parametrize(("fmt", "dial_fields"), [(PCMU, {}), (L16_WIDE, {"stream_codec": "L16"})])
async def test_wav_greeting_is_resampled_to_the_stream(
    media_agent: MediaAgent, tmp_path: Path, fmt: MediaFormat, dial_fields: dict[str, Any]
) -> None:
    wav = tone_wav(tmp_path / "hello.wav", 1000, 300)
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        ccid = await scripted_call(
            client, state, media_agent, scenario_text(greeting=wav.as_posix()), **dial_fields
        )
        script = await finished(state, ccid)
        greeting = script["greeting"]
        assert (greeting["source"], greeting["status"], greeting["utterance_id"]) == ("wav", "ok", None)
        [entry] = leg(state, ccid).timeline_utterances
        assert (entry["utterance_id"], entry["text"]) == (None, "hello.wav")
        assert entry["end_ms"] - entry["start_ms"] >= 280
        pcm = caller_pcm(media_agent, fmt)
        assert tone_power(pcm, 1000, fmt.sample_rate) > 100 * tone_power(pcm, 2500, fmt.sample_rate)


async def test_missing_wav_greeting_fails_and_steps_still_run(
    media_agent: MediaAgent, tmp_path: Path
) -> None:
    missing = (tmp_path / "missing.wav").as_posix()
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        scenario = scenario_text(["say: {text: still here}"], greeting=missing)
        ccid = await scripted_call(client, state, media_agent, scenario)
        script = await finished(state, ccid)
        greeting = script["greeting"]
        assert greeting["status"] == "failed"
        assert "missing.wav" in greeting["detail"]
        assert greeting["started_ms"] == greeting["ended_ms"]
        assert script["steps"][0]["status"] == "ok"
        assert script["status"] == "done"
        assert [entry["text"] for entry in leg(state, ccid).timeline_utterances] == ["still here"]


# Agent speech.


async def test_wait_agent_speech_completes_after_the_turn_and_silence(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        steps = ["wait_agent_speech: {min_ms: 300, then_silence_ms: 700, timeout_ms: 8000}"]
        ccid = await scripted_call(client, state, media_agent, scenario_text(steps))
        await speak(media_agent, loud(600))
        script = await finished(state, ccid)
        [step] = script["steps"]
        assert (step["index"], step["type"], step["status"], step["detail"]) == (
            0,
            "wait_agent_speech",
            "ok",
            None,
        )
        call = leg(state, ccid)
        speech = agent_speech_frames(call)
        assert len(speech) == 30
        speech_end = track_ms(call, speech[-1]) + 20
        assert 680 <= step["ended_ms"] - speech_end < 720
        assert step["started_ms"] <= track_ms(call, speech[0])


async def test_short_blip_times_out_and_the_next_step_runs(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        steps = [
            "wait_agent_speech: {min_ms: 500, then_silence_ms: 300, timeout_ms: 2000}",
            "say: {text: after}",
        ]
        ccid = await scripted_call(client, state, media_agent, scenario_text(steps))
        await speak(media_agent, loud(100))
        script = await finished(state, ccid)
        waited, said = script["steps"]
        assert waited["status"] == "timeout"
        assert waited["detail"] == "no agent turn of 500 ms followed by 300 ms of silence within 2000 ms"
        assert waited["ended_ms"] - waited["started_ms"] == 2000
        assert said["status"] == "ok"
        assert said["started_ms"] == waited["ended_ms"]


async def test_zero_then_silence_completes_once_the_turn_qualifies(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        steps = ["wait_agent_speech: {min_ms: 200, then_silence_ms: 0}"]
        ccid = await scripted_call(client, state, media_agent, scenario_text(steps))
        await speak(media_agent, loud(1000))
        script = await finished(state, ccid)
        [step] = script["steps"]
        call = leg(state, ccid)
        first = track_ms(call, agent_speech_frames(call)[0])
        assert step["status"] == "ok"
        assert step["ended_ms"] - first == 180


# Say.


async def test_say_completes_when_its_last_byte_is_sent(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        steps = ["say: {text: 'Hi, yes this is Ana.', language: es}"]
        ccid = await scripted_call(client, state, media_agent, scenario_text(steps))
        script = await finished(state, ccid)
        [step] = script["steps"]
        [entry] = leg(state, ccid).timeline_utterances
        assert step["status"] == "ok"
        assert step["utterance_id"] == entry["utterance_id"]
        assert step["ended_ms"] == entry["end_ms"]
        assert entry["text"] == "Hi, yes this is Ana."
        assert state.utterances[step["utterance_id"]].language == "es"
        assert decode_marker(caller_pcm(media_agent, PCMU), 8000) == step["utterance_id"]
        detail = await summary(client, ccid)
        timeline = (await client.get(f"/sim/telnyx/calls/{ccid}/timeline")).json()
        assert detail["script"] == timeline["script"] == script


async def test_barge_in_starts_while_the_agent_is_still_talking(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        steps = ["say: {text: 'Sorry, one thing', barge_in_after_agent_ms: 500}"]
        ccid = await scripted_call(client, state, media_agent, scenario_text(steps))
        await speak(media_agent, loud(2000))
        script = await finished(state, ccid)
        [step] = script["steps"]
        call = leg(state, ccid)
        [entry] = call.timeline_utterances
        speech = agent_speech_frames(call)
        assert step["status"] == "ok"
        assert 500 <= entry["start_ms"] - track_ms(call, speech[0]) <= 540
        assert entry["start_ms"] < track_ms(call, speech[-1])


async def test_barge_in_without_agent_speech_times_out_silently(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        steps = ["say: {text: 'never said', barge_in_after_agent_ms: 0}"]
        ccid = await scripted_call(client, state, media_agent, scenario_text(steps))
        script = await finished(state, ccid)
        [step] = script["steps"]
        assert step["status"] == "timeout"
        assert step["detail"] == "the agent did not speak for 20 ms within 10000 ms"
        assert step["ended_ms"] - step["started_ms"] == 10_000
        assert step["utterance_id"] is None
        assert leg(state, ccid).timeline_utterances == []


async def test_say_fails_when_utterance_ids_run_out(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        state.utterances.update({n: Utterance("x", "en", ("x",)) for n in range(1, MAX_UTTERANCE_ID + 1)})
        ccid = await scripted_call(client, state, media_agent, scenario_text(["say: {text: one more}"]))
        script = await finished(state, ccid)
        [step] = script["steps"]
        assert step["status"] == "failed"
        assert "utterance ids" in step["detail"]
        assert script["status"] == "done"


# DTMF and silence.


async def test_dtmf_events_with_a_pause(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        ccid = await scripted_call(client, state, media_agent, scenario_text(["dtmf: {digits: '12w3'}"]))
        script = await finished(state, ccid)
        assert script["steps"][0]["status"] == "ok"
        await media_agent.wait_for(lambda agent: len(agent.of("dtmf")) == 3)
        dtmf = media_agent.of("dtmf")
        assert [frame["dtmf"]["digit"] for frame in dtmf] == ["1", "2", "3"]
        first, second, third = (media_agent.frames.index(frame) for frame in dtmf)
        assert [frame["event"] for frame in media_agent.frames[first + 1 : second]] == ["media"]
        between = [frame for frame in media_agent.frames[second:third] if frame["event"] == "media"]
        assert len(between) >= 25
        caller_dtmf = (await summary(client, ccid))["caller_dtmf"]
        assert [entry["digit"] for entry in caller_dtmf] == ["1", "2", "3"]
        assert timestamp_s(caller_dtmf[1]["at"]) - timestamp_s(caller_dtmf[0]["at"]) == pytest.approx(0.02)
        assert timestamp_s(caller_dtmf[2]["at"]) - timestamp_s(caller_dtmf[1]["at"]) >= 0.5


async def test_inband_dtmf_sends_tones_only(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        steps = ["dtmf: {digits: '5', inband: true}"]
        ccid = await scripted_call(client, state, media_agent, scenario_text(steps))
        script = await finished(state, ccid)
        [step] = script["steps"]
        assert step["status"] == "ok"
        assert step["ended_ms"] - step["started_ms"] >= 180
        assert media_agent.of("dtmf") == []
        assert (await summary(client, ccid))["caller_dtmf"] == []
        pcm = caller_pcm(media_agent, PCMU)
        noise = tone_power(pcm, 1000, 8000)
        assert tone_power(pcm, 770, 8000) > 100 * noise
        assert tone_power(pcm, 1336, 8000) > 100 * noise


async def test_silence_keeps_the_caller_quiet(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        steps = ["say: {text: one}", "silence: {ms: 1000}", "say: {text: two}"]
        ccid = await scripted_call(client, state, media_agent, scenario_text(steps))
        script = await finished(state, ccid)
        quiet = script["steps"][1]
        assert quiet["ended_ms"] - quiet["started_ms"] == 1000
        first, second = leg(state, ccid).timeline_utterances
        assert second["start_ms"] - first["end_ms"] >= 1000


# Hang-ups.


async def test_expect_hangup_by_agent(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        steps = ["expect_hangup: {by: agent, timeout_ms: 5000}"]
        ccid = await scripted_call(client, state, media_agent, scenario_text(steps))
        await ok(client, ccid, "hangup")
        script = await finished(state, ccid)
        assert script["status"] == "done"
        assert script["steps"][0]["status"] == "ok"


async def test_expect_hangup_by_caller_fails_when_the_agent_hangs_up(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        steps = ["expect_hangup: {by: caller}"]
        ccid = await scripted_call(client, state, media_agent, scenario_text(steps))
        await ok(client, ccid, "hangup")
        script = await finished(state, ccid)
        [step] = script["steps"]
        assert (step["status"], step["detail"]) == ("failed", "hung up by caller")


async def test_expect_hangup_times_out(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        steps = ["expect_hangup: {timeout_ms: 1000}"]
        ccid = await scripted_call(client, state, media_agent, scenario_text(steps))
        script = await finished(state, ccid)
        [step] = script["steps"]
        assert (step["status"], step["detail"]) == ("timeout", "the call did not end within 1000 ms")
        assert step["ended_ms"] - step["started_ms"] == 1000
        assert not leg(state, ccid).ended


async def test_hangup_step_ends_an_outgoing_call_as_the_callee(media_agent: MediaAgent) -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        ccid = await scripted_call(
            client, state, media_agent, scenario_text(["say: {text: bye}", "hangup: {}"])
        )
        script = await finished(state, ccid)
        await settle(state)
        assert script["status"] == "done"
        assert [step["status"] for step in script["steps"]] == ["ok", "ok"]
        [hangup] = payloads(transport, "call.hangup")
        assert (hangup["hangup_cause"], hangup["hangup_source"]) == ("normal_clearing", "callee")
        assert media_agent.frames[-1]["event"] == "stop"


async def test_hangup_step_ends_an_inbound_call_as_the_caller(media_agent: MediaAgent) -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        register(state, scenario_text(["hangup: {}"], inbound=True))
        media_agent.clock = state.clock
        app = await create_app(client)
        ccid = (await inbound_call(client, state, app["id"]))["call_control_id"]
        await ok(client, ccid, "answer", stream_url=media_agent.url)
        script = await finished(state, ccid)
        await settle(state)
        assert script["status"] == "done"
        [hangup] = payloads(transport, "call.hangup")
        assert hangup["hangup_source"] == "caller"


# Transfer targets.


async def test_transfer_target_steps_run_on_the_original_stream(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        target_steps = "[{say: {text: 'Hi, this is Bob from sales.'}}]"
        extra = f"transfer_target:\n  answer_after_ms: 1000\n  steps: {target_steps}\n"
        ccid = await scripted_call(client, state, media_agent, scenario_text(extra=extra))
        assert script_of(state, ccid) is None
        await ok(client, ccid, "transfer", to=TRANSFER_TO)
        [transfer] = (await summary(client, ccid))["transfers"]
        target = transfer["call_control_id"]
        script = await finished(state, target)
        assert script["role"] == "transfer_target"
        assert script["greeting"] is None
        [step] = script["steps"]
        assert step["status"] == "ok"
        [entry] = leg(state, ccid).timeline_utterances
        assert (entry["utterance_id"], entry["text"]) == (step["utterance_id"], "Hi, this is Bob from sales.")
        assert entry["start_ms"] >= leg(state, target).answered_ms
        assert decode_marker(caller_pcm(media_agent, PCMU), 8000) == step["utterance_id"]
        assert (await summary(client, target))["script"] == script


async def test_link_to_leg_without_its_own_scenario_is_a_transfer_target(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        extra = "transfer_target:\n  answer_after_ms: 500\n  steps: [{say: {text: linked}}]\n"
        ccid = await scripted_call(client, state, media_agent, scenario_text(extra=extra))
        app = leg(state, ccid).connection_id
        linked = await dial(client, app, to=TRANSFER_TO, link_to=ccid, bridge_on_answer=True)
        script = await finished(state, linked["call_control_id"])
        assert script["role"] == "transfer_target"
        assert script["steps"][0]["status"] == "ok"


# Interruption and reporting.


@pytest.mark.parametrize(
    ("action", "detail", "ended"),
    [("hangup", "call ended", True), ("streaming_stop", "media stream stopped", False)],
)
async def test_interruption_cancels_the_running_and_pending_steps(
    media_agent: MediaAgent, action: str, detail: str, ended: bool
) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        steps = ["wait_agent_speech: {timeout_ms: 8000}", "say: {text: never}"]
        ccid = await scripted_call(client, state, media_agent, scenario_text(steps))
        await until(state, lambda: (script_of(state, ccid) or {})["steps"][0]["status"] == "running")
        await ok(client, ccid, action)
        script = await finished(state, ccid)
        waited, said = script["steps"]
        assert script["status"] == "cancelled"
        assert (waited["status"], waited["detail"]) == ("cancelled", detail)
        assert waited["ended_ms"] is not None
        assert (said["status"], said["detail"], said["started_ms"]) == ("cancelled", detail, None)
        assert leg(state, ccid).ended is ended


async def test_greeting_cancelled_when_the_call_ends_mid_utterance(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        greeting = "Hello, this is a rather long greeting that keeps going for a few seconds."
        ccid = await scripted_call(client, state, media_agent, scenario_text(greeting=greeting))
        await until(state, lambda: (script_of(state, ccid) or {})["greeting"]["status"] == "running")
        await ok(client, ccid, "hangup")
        script = await finished(state, ccid)
        assert (script["greeting"]["status"], script["greeting"]["detail"]) == ("cancelled", "call ended")


async def test_no_script_for_a_scenario_without_steps_or_greeting(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        ccid = await scripted_call(client, state, media_agent, scenario_text())
        await media_agent.wait_media(3)
        assert (await summary(client, ccid))["script"] is None


async def test_no_script_without_a_scenario(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        media_agent.clock = state.clock
        app = await create_app(client)
        ccid = (await dial(client, app["id"], stream_url=media_agent.url))["call_control_id"]
        await at(state, 1.0)
        await media_agent.wait_media(3)
        assert script_of(state, ccid) is None


async def test_reset_mid_script_closes_the_stream(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        steps = ["wait_agent_speech: {timeout_ms: 8000}"]
        await scripted_call(client, state, media_agent, scenario_text(steps))
        response = await client.post("/sim/reset", json={})
        assert response.status_code == 204, response.text
        await media_agent.wait_closed()
        await state.tasks.drain()
        assert state.tasks.pending == 0
