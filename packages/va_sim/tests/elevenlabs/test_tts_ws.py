"""FakeElevenLabs TTS multi-context WebSocket over real sockets: auth, query validation, generation, contexts,
close_context / close_socket, keepalive, inactivity, faults, reset, and the request records."""

from __future__ import annotations

import asyncio
import base64
import time

import pytest
from tts_helpers import (
    API_KEY,
    audio_frames,
    joined_audio,
    realtime_state,
    recv_json,
    recv_spoken,
    recv_until_final,
    send,
    served,
    text_of,
    tts_socket,
    wait_closed,
)

from va_sim.clock import AcceleratedClock
from va_sim.elevenlabs.tts_audio import parse_output_format
from va_sim.settings import Latencies
from va_sim.state import SimState

ULAW = {"output_format": "ulaw_8000"}
LONG_TEXT = "The quick brown fox jumps over the lazy dog. " * 4  # 180 chars, 10.8 s, 54 chunks


def accelerated_state() -> SimState:
    latencies = Latencies(stt_final_ms=10, tts_ttfa_ms=20, ttft_ms=20, tokens_per_s=1000.0)
    return SimState(clock=AcceleratedClock(), latencies=latencies)


# Authentication and query.


async def test_header_key_authenticates_and_flush_returns_audio() -> None:
    state = realtime_state()
    async with served(state) as base, tts_socket(base, ULAW) as ws:
        await send(ws, {"text": "Hello there. ", "context_id": "c1", "flush": True})
        frames = await recv_spoken(ws, "c1", "Hello there. ")
    audio = audio_frames(frames, "c1")
    assert len(joined_audio(audio)) == 13 * 480  # 60 ms of 8 kHz μ-law per char
    assert text_of(audio) == "Hello there. "
    assert set(audio[0]) == {"audio", "contextId", "alignment", "normalizedAlignment"}
    assert list(audio[0]) == ["audio", "contextId", "alignment", "normalizedAlignment"]
    assert audio[0]["alignment"] == audio[0]["normalizedAlignment"]
    assert frames[-1] == {"isFinal": True, "contextId": "c1"}


@pytest.mark.parametrize(
    "auth", [{"xi_api_key": API_KEY}, {"authorization": f"Bearer {API_KEY}"}, {"authorization": API_KEY}]
)
async def test_first_message_key_authenticates(auth: dict[str, str]) -> None:
    state = realtime_state()
    async with served(state) as base, tts_socket(base, ULAW, key=None) as ws:
        await send(ws, {"text": "Hi. ", "context_id": "a", "flush": True, **auth})
        frames = await recv_spoken(ws, "a", "Hi. ")
    assert text_of(audio_frames(frames)) == "Hi. "


async def test_missing_key_closes_1008() -> None:
    async with served(realtime_state()) as base, tts_socket(base, ULAW, key=None) as ws:
        await send(ws, {"text": " ", "context_id": "a"})
        closed = await wait_closed(ws)
    assert (closed.code, closed.reason) == (1008, "Invalid API key")


async def test_allow_list_rejects_other_keys() -> None:
    state = realtime_state()
    state.elevenlabs.api_keys = ("good-key",)
    async with served(state) as base:
        async with tts_socket(base, ULAW, key="other-key") as ws:
            closed = await wait_closed(ws)
        assert (closed.code, closed.reason) == (1008, "Invalid API key")
        async with tts_socket(base, ULAW, key="good-key") as ws:
            await send(ws, {"text": "ok ", "context_id": "a", "flush": True})
            assert (await recv_json(ws))["contextId"] == "a"


@pytest.mark.parametrize(
    ("query", "reason"),
    [
        ({"output_format": "opus_48000_64"}, "invalid output_format: unsupported format"),
        ({"inactivity_timeout": "0"}, "invalid inactivity_timeout: must be between 1 and 180"),
        ({"inactivity_timeout": "181"}, "invalid inactivity_timeout: must be between 1 and 180"),
        ({"inactivity_timeout": "soon"}, "invalid inactivity_timeout: must be an integer"),
        ({"sync_alignment": "yes"}, "invalid sync_alignment: must be true or false"),
        (
            {"apply_text_normalization": "always"},
            "invalid apply_text_normalization: must be auto, on, or off",
        ),
        ({"model_id": ""}, "invalid model_id: must not be empty"),
    ],
)
async def test_invalid_query_closes_1008(query: dict[str, str], reason: str) -> None:
    async with served(realtime_state()) as base, tts_socket(base, query) as ws:
        closed = await wait_closed(ws)
    assert (closed.code, closed.reason) == (1008, reason)


async def test_invalid_json_closes_1008() -> None:
    async with served(realtime_state()) as base, tts_socket(base, ULAW) as ws:
        await ws.send("{not json")
        closed = await wait_closed(ws)
    assert (closed.code, closed.reason) == (1008, "invalid message")


async def test_non_object_first_message_closes_1008() -> None:
    async with served(realtime_state()) as base, tts_socket(base, ULAW, key=None) as ws:
        await ws.send("[1, 2]")
        closed = await wait_closed(ws)
    assert (closed.code, closed.reason) == (1008, "invalid message")


# Generation.


async def test_default_format_is_mp3_and_alignment_reconstructs_text() -> None:
    state = realtime_state()
    async with served(state) as base, tts_socket(base) as ws:
        await send(ws, {"text": "mp3 please ", "flush": True})
        await send(ws, {"close_socket": True})
        closed = await wait_closed(ws)
    audio = audio_frames(closed.frames, "default")
    assert joined_audio(audio)[:2] == b"\xff\xfb"
    assert text_of(audio) == "mp3 please "
    assert closed.code == 1000
    record = state.elevenlabs.tts_requests[0]
    assert (record.output_format, record.model_id, record.context_id) == (
        "mp3_44100_128",
        "eleven_flash_v2_5",
        "default",
    )


async def test_first_audio_waits_ttfa() -> None:
    state = realtime_state(tts_ttfa_ms=200)
    async with served(state) as base, tts_socket(base, ULAW) as ws:
        started = time.monotonic()
        await send(ws, {"text": "Wait for it. ", "context_id": "a", "flush": True})
        first = await recv_json(ws)
        elapsed = time.monotonic() - started
    assert "audio" in first
    assert elapsed >= 0.2


async def test_interleaved_contexts_both_complete_in_order() -> None:
    state = realtime_state()
    async with served(state) as base, tts_socket(base, ULAW) as ws:
        await send(ws, {"text": "First context speaking now. ", "context_id": "A"})
        await send(ws, {"text": "Second one talks too. ", "context_id": "B"})
        await send(ws, {"context_id": "A", "flush": True})
        await send(ws, {"context_id": "B", "flush": True})
        await send(ws, {"close_socket": True})
        closed = await wait_closed(ws)
    a_frames = audio_frames(closed.frames, "A")
    b_frames = audio_frames(closed.frames, "B")
    assert text_of(a_frames) == "First context speaking now. "
    assert text_of(b_frames) == "Second one talks too. "
    assert len(a_frames) == 9
    assert len(b_frames) == 7


async def test_schedule_triggers_generation_without_flush() -> None:
    state = realtime_state()
    async with served(state) as base, tts_socket(base, ULAW) as ws:
        await send(
            ws,
            {"text": " ", "context_id": "s", "generation_config": {"chunk_length_schedule": [50]}},
        )
        await send(ws, {"text": "x" * 50, "context_id": "s"})
        first = await recv_json(ws)
    assert first["contextId"] == "s"
    assert first["alignment"]["chars"][:2] == [" ", "x"]


async def test_keepalive_produces_no_audio() -> None:
    state = realtime_state()
    async with served(state) as base, tts_socket(base, ULAW) as ws:
        await send(ws, {"text": " ", "context_id": "ka"})
        await send(ws, {"text": "", "context_id": "ka"})
        await send(ws, {"text": "", "context_id": "ka", "flush": True})
        await send(ws, {"text": "real ", "context_id": "u", "flush": True})
        first = await recv_json(ws)
    assert first["contextId"] == "u"
    keepalive = state.elevenlabs.tts_requests[0]
    assert (keepalive.context_id, keepalive.text, keepalive.characters) == ("ka", " ", 0)


async def test_close_context_mid_stream_stops_audio() -> None:
    state = realtime_state()
    # Hold the stream after its first chunk long enough for the close to arrive mid-generation.
    state.elevenlabs.arm_fault("delay", after_chunks=1, delay_ms=300, times=1)
    async with served(state) as base, tts_socket(base, ULAW) as ws:
        await send(ws, {"text": LONG_TEXT, "context_id": "long", "flush": True})
        first = await recv_json(ws)
        assert first["contextId"] == "long"
        await send(ws, {"context_id": "long", "close_context": True})
        frames = [first, *await recv_until_final(ws, "long")]
        await send(ws, {"text": "after ", "context_id": "next", "flush": True})
        after = await recv_json(ws)
    assert len(audio_frames(frames)) == 1
    assert after["contextId"] == "next"
    record = state.elevenlabs.tts_requests[0]
    assert record.interrupted is True
    assert record.end_reason == "close_context"
    assert record.ended_ms is not None


async def test_close_context_with_unflushed_text_sends_only_final() -> None:
    state = realtime_state()
    async with served(state) as base, tts_socket(base, ULAW) as ws:
        await send(ws, {"text": "never spoken", "context_id": "u"})
        await send(ws, {"context_id": "u", "close_context": True})
        frames = await recv_until_final(ws, "u")
    assert frames == [{"isFinal": True, "contextId": "u"}]
    record = state.elevenlabs.tts_requests[0]
    assert (record.text, record.generated_text, record.interrupted) == ("never spoken", "", True)


async def test_completed_context_close_is_not_interrupted() -> None:
    state = realtime_state()
    async with served(state) as base, tts_socket(base, ULAW) as ws:
        await send(ws, {"text": "done. ", "context_id": "d", "flush": True})
        frames = await recv_spoken(ws, "d", "done. ")
    assert frames[-1] == {"isFinal": True, "contextId": "d"}
    assert state.elevenlabs.tts_requests[0].interrupted is False


async def test_sixth_context_is_refused_and_socket_stays_open() -> None:
    state = realtime_state()
    async with served(state) as base, tts_socket(base, ULAW) as ws:
        for index in range(5):
            await send(ws, {"text": " ", "context_id": f"c{index}"})
        await send(ws, {"text": "too many ", "context_id": "c5", "flush": True})
        refused = await recv_json(ws)
        await send(ws, {"text": "still here ", "context_id": "c0", "flush": True})
        alive = await recv_json(ws)
    assert refused == {
        "message": "Maximum of 5 concurrent contexts per connection exceeded",
        "error": "max_active_conversations",
        "code": 1008,
    }
    assert alive["contextId"] == "c0"
    assert len(state.elevenlabs.tts_requests) == 5


async def test_closed_context_id_cannot_be_reused() -> None:
    async with served(realtime_state()) as base, tts_socket(base, ULAW) as ws:
        await send(ws, {"text": " ", "context_id": "x"})
        await send(ws, {"context_id": "x", "close_context": True})
        await recv_until_final(ws, "x")
        await send(ws, {"text": "again ", "context_id": "x", "flush": True})
        error = await recv_json(ws)
    assert error["error"] == "context_closed"
    assert error["code"] == 1008


@pytest.mark.parametrize(
    "message",
    [
        {"text": "x", "context_id": "a", "generation_config": {"chunk_length_schedule": [10]}},
        {"text": 5, "context_id": "a"},
        {"text": "x", "context_id": 7},
    ],
)
async def test_invalid_fields_send_an_error_frame(message: dict[str, object]) -> None:
    async with served(realtime_state()) as base, tts_socket(base, ULAW) as ws:
        await send(ws, message)
        error = await recv_json(ws)
        await send(ws, {"text": "fine ", "context_id": "b", "flush": True})
        fine = await recv_json(ws)
    assert error["error"] == "invalid_message"
    assert fine["contextId"] == "b"


async def test_close_socket_finishes_in_flight_and_finals_every_context() -> None:
    state = realtime_state()
    async with served(state) as base, tts_socket(base, ULAW) as ws:
        await send(ws, {"text": "Finish this sentence please. ", "context_id": "a", "flush": True})
        await send(ws, {"text": "unflushed", "context_id": "b"})
        await send(ws, {"close_socket": True})
        closed = await wait_closed(ws)
    assert closed.code == 1000
    assert text_of(audio_frames(closed.frames, "a")) == "Finish this sentence please. "
    assert audio_frames(closed.frames, "b") == []
    finals = [frame for frame in closed.frames if frame.get("isFinal")]
    assert {frame["contextId"] for frame in finals} == {"a", "b"}
    a, b = state.elevenlabs.tts_requests
    assert (a.end_reason, a.interrupted) == ("close_socket", False)
    assert (b.end_reason, b.interrupted) == ("close_socket", True)


async def test_disconnect_ends_open_records() -> None:
    state = realtime_state()
    async with served(state) as base:
        async with tts_socket(base, ULAW) as ws:
            await send(ws, {"text": "pending", "context_id": "a"})
            await send(ws, {"text": "", "context_id": "ka"})
        async with tts_socket(base, ULAW) as probe:
            await send(probe, {"text": "x ", "context_id": "p", "flush": True})
            await recv_json(probe)
    record = state.elevenlabs.tts_requests[0]
    assert (record.end_reason, record.interrupted) == ("disconnect", True)


# Inactivity.


@pytest.mark.parametrize(("query", "seconds"), [({}, 20), ({"inactivity_timeout": "5"}, 5)])
async def test_inactivity_closes_1008(query: dict[str, str], seconds: int) -> None:
    state = accelerated_state()
    async with served(state) as base, tts_socket(base, {**ULAW, **query}) as ws:
        closed = await wait_closed(ws)
    assert closed.code == 1008
    assert closed.reason == f"Have not received a new text input within the timeout of {seconds} seconds."
    assert state.clock.virtual_ms() >= seconds * 1000


async def test_inactivity_ends_records_and_keepalives_hold_it_off() -> None:
    state = realtime_state()
    async with served(state) as base, tts_socket(base, {**ULAW, "inactivity_timeout": "1"}) as ws:
        started = time.monotonic()
        while time.monotonic() - started < 1.5:
            await send(ws, {"text": "", "context_id": "ka"})
            await asyncio.sleep(0.4)
        await send(ws, {"text": "left over", "context_id": "a"})
        closed = await wait_closed(ws)
        lasted = time.monotonic() - started
    assert closed.code == 1008
    assert lasted >= 1.5
    ka, a = state.elevenlabs.tts_requests
    assert (ka.end_reason, ka.interrupted) == ("inactivity", False)
    assert (a.end_reason, a.interrupted) == ("inactivity", True)


# Faults.


async def test_fault_disconnect_after_one_chunk() -> None:
    state = realtime_state()
    state.elevenlabs.arm_fault("disconnect", after_chunks=1, delay_ms=0, times=1)
    async with served(state) as base, tts_socket(base, ULAW) as ws:
        await send(ws, {"text": LONG_TEXT, "context_id": "f", "flush": True})
        closed = await wait_closed(ws)
    assert (closed.code, closed.reason) == (1011, "simulated disconnect")
    assert len(audio_frames(closed.frames)) == 1
    assert state.elevenlabs.fault is None
    record = state.elevenlabs.tts_requests[0]
    assert (record.end_reason, record.interrupted) == ("fault", True)


async def test_fault_error_sends_a_frame_then_closes() -> None:
    state = realtime_state()
    state.elevenlabs.arm_fault("error", after_chunks=0, delay_ms=0, times=1)
    async with served(state) as base, tts_socket(base, ULAW) as ws:
        await send(ws, {"text": "boom ", "context_id": "e", "flush": True})
        closed = await wait_closed(ws)
    assert closed.frames == ({"message": "Simulated TTS failure", "error": "internal_error", "code": 1011},)
    assert closed.code == 1011


async def test_fault_delay_is_consumed_then_cleared() -> None:
    state = realtime_state()
    state.elevenlabs.arm_fault("delay", after_chunks=0, delay_ms=150, times=2)
    async with served(state) as base, tts_socket(base, ULAW) as ws:
        timings = []
        for context_id in ("d1", "d2", "d3"):
            started = time.monotonic()
            await send(ws, {"text": "hi ", "context_id": context_id, "flush": True})
            frame = await recv_json(ws)
            assert frame["contextId"] == context_id
            timings.append(time.monotonic() - started)
            if context_id == "d1":
                assert state.elevenlabs.fault is not None
                assert state.elevenlabs.fault.remaining == 1
            if context_id == "d2":
                assert state.elevenlabs.fault is None
    assert timings[0] >= 0.15
    assert timings[1] >= 0.15


# Reset and records.


async def test_reset_closes_open_sockets_1012() -> None:
    state = realtime_state()
    async with served(state) as base, tts_socket(base, ULAW) as ws:
        await send(ws, {"text": "x ", "context_id": "a", "flush": True})
        await recv_json(ws)
        assert state.snapshot()["elevenlabs"] == {"tts_requests": 1, "tts_sockets": 1}
        state.reset()
        closed = await wait_closed(ws)
    assert (closed.code, closed.reason) == (1012, "simulator reset")
    assert state.tasks.pending == 0
    assert state.elevenlabs.tts_requests == []
    assert state.elevenlabs.sockets == set()


async def test_records_capture_text_and_generation() -> None:
    state = realtime_state()
    async with (
        served(state) as base,
        tts_socket(base, {**ULAW, "model_id": "eleven_turbo_v2_5"}, voice_id="Karen") as ws,
    ):
        await send(ws, {"text": "One ", "context_id": "r"})
        await send(ws, {"text": "two. ", "context_id": "r", "flush": True})
        await send(ws, {"text": "three", "context_id": "r"})
        frames = await recv_spoken(ws, "r", "One two. ")
    assert frames[-1]["isFinal"] is True
    record = state.elevenlabs.tts_requests[0].to_json()
    assert record == {
        "id": "ttsreq_1",
        "transport": "ws",
        "voice_id": "Karen",
        "model_id": "eleven_turbo_v2_5",
        "output_format": "ulaw_8000",
        "context_id": "r",
        "started_ms": record["started_ms"],
        "text": "One two. three",
        "generated_text": "One two. ",
        "characters": 9,
        "ended_ms": record["ended_ms"],
        "interrupted": True,
        "end_reason": "close_context",
    }


async def test_pcm_audio_over_the_socket_matches_the_generator() -> None:
    state = realtime_state()
    fmt = parse_output_format("pcm_24000")
    async with served(state) as base, tts_socket(base, {"output_format": "pcm_24000"}) as ws:
        await send(ws, {"text": "pcm ", "context_id": "p", "flush": True})
        frame = await recv_json(ws)
    assert len(base64.b64decode(frame["audio"])) == 24_000 * 200 // 1000 * fmt.bytes_per_sample


async def test_binary_json_frames_are_accepted() -> None:
    async with served(realtime_state()) as base, tts_socket(base, ULAW) as ws:
        await ws.send(b'{"text": "bytes ", "context_id": "b", "flush": true}')
        frame = await recv_json(ws)
    assert frame["contextId"] == "b"


async def test_close_context_drops_queued_generations() -> None:
    state = realtime_state()
    state.elevenlabs.arm_fault("delay", after_chunks=0, delay_ms=300, times=1)
    async with served(state) as base, tts_socket(base, ULAW) as ws:
        await send(ws, {"text": "first ", "context_id": "q", "flush": True})
        await send(ws, {"text": "second ", "context_id": "q", "flush": True})
        await send(ws, {"text": "third ", "context_id": "q", "flush": True})
        await send(ws, {"context_id": "q", "close_context": True})
        frames = await recv_until_final(ws, "q")
    assert text_of(audio_frames(frames, "q")) in {"", "first "}
    assert "second" not in text_of(audio_frames(frames, "q"))
    record = state.elevenlabs.tts_requests[0]
    assert (record.end_reason, record.interrupted) == ("close_context", True)


async def test_disconnect_before_authenticating_records_nothing() -> None:
    state = realtime_state()
    async with served(state) as base:
        async with tts_socket(base, ULAW, key=None):
            pass
        async with tts_socket(base, ULAW) as probe:
            await send(probe, {"text": "x ", "context_id": "p", "flush": True})
            await recv_json(probe)
    assert [record.context_id for record in state.elevenlabs.tts_requests] == ["p"]
