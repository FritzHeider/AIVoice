"""FakeElevenLabs realtime STT WebSocket over real sockets: auth, query validation, session_started,
synthesized caller audio to partials and commits (μ-law 8 kHz and PCM 16 kHz), timestamps, entities, input
errors, the transcript script, vad, faults, reset, and the session records."""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest
from stt_helpers import ULAW, caller_audio, chunked, recv_type, send_audio, stt_socket
from tts_helpers import Closed, realtime_state, recv_json, send, served, wait_closed
from websockets.asyncio.client import ClientConnection

from va_sim.elevenlabs.stt_params import AUDIO_FORMATS
from va_sim.elevenlabs.stt_store import FATAL_1008, FATAL_1011, STT_FAULT_KINDS, SttFaultKind
from va_sim.state import SimState

TEXT = "I would like to book a table for two"
PCM16 = {"model_id": "scribe_v2_realtime", "audio_format": "pcm_16000"}
SILENCE_100MS = b"\xff" * 800
LOUD_600MS = b"\x10\x90" * 2_400  # alternating high-magnitude μ-law codes: voiced, but no marker
NON_FATAL: tuple[SttFaultKind, ...] = tuple(
    kind
    for kind in STT_FAULT_KINDS
    if kind not in FATAL_1008 | FATAL_1011 | {"disconnect", "commit_throttled"}
)


async def started(ws: ClientConnection) -> dict[str, object]:
    frame = await recv_json(ws)
    assert frame["message_type"] == "session_started"
    return frame


# Session start, authentication, query.


async def test_session_started_echoes_the_config() -> None:
    state = realtime_state()
    query = {**ULAW, "keyterms": ["Acme", "Karen"], "include_timestamps": "true", "language_code": "en"}
    async with served(state) as base, stt_socket(base, query) as ws:
        frame = await started(ws)
    session_id = frame["session_id"]
    assert isinstance(session_id, str)
    assert len(session_id) == 32
    assert int(session_id, 16) >= 0
    config = frame["config"]
    assert isinstance(config, dict)
    assert config["model_id"] == "scribe_v2_realtime"
    assert (config["audio_format"], config["sample_rate"]) == ("ulaw_8000", 8_000)
    assert config["keyterms"] == ["Acme", "Karen"]
    assert config["include_timestamps"] is True
    assert config["language_code"] == "en"
    assert state.elevenlabs.stt.sessions[0].id == session_id


async def test_missing_key_sends_auth_error_and_closes_1008() -> None:
    state = realtime_state()
    async with served(state) as base, stt_socket(base, key=None) as ws:
        closed = await wait_closed(ws)
    assert (closed.code, closed.reason) == (1008, "Missing API key")
    (frame,) = closed.frames
    assert frame["message_type"] == "auth_error"
    assert "xi-api-key" in frame["error"]
    assert state.elevenlabs.stt.sessions == []


async def test_rejected_key_sends_auth_error_and_closes_1008() -> None:
    state = realtime_state()
    state.elevenlabs.api_keys = ("good-key",)
    async with served(state) as base, stt_socket(base, key="other-key") as ws:
        closed = await wait_closed(ws)
    assert (closed.code, closed.reason) == (1008, "Invalid API key")
    assert closed.frames == ({"message_type": "auth_error", "error": "Invalid API key"},)


async def test_invalid_query_sends_invalid_request_and_closes_1008() -> None:
    async with served(realtime_state()) as base, stt_socket(base, {**ULAW, "commit_strategy": "auto"}) as ws:
        closed = await wait_closed(ws)
    reason = "invalid commit_strategy: must be manual or vad"
    assert (closed.code, closed.reason) == (1008, reason)
    assert closed.frames == ({"message_type": "invalid_request", "error": reason},)


async def test_query_is_validated_before_auth() -> None:
    async with (
        served(realtime_state()) as base,
        stt_socket(base, {"audio_format": "ulaw_8000"}, key=None) as ws,
    ):
        closed = await wait_closed(ws)
    assert closed.reason == "invalid model_id: required"


# Transcription.


@pytest.mark.parametrize("query", [ULAW, PCM16], ids=["ulaw_8000", "pcm_16000"])
async def test_caller_audio_is_transcribed(query: dict[str, str]) -> None:
    state = realtime_state()
    fmt = AUDIO_FORMATS[query["audio_format"]]
    audio = caller_audio(state, TEXT, fmt.name)
    async with served(state) as base, stt_socket(base, {**query, "include_timestamps": "true"}) as ws:
        await started(ws)
        for piece in chunked(audio, fmt):
            await send_audio(ws, piece)
        await send_audio(ws, b"", commit=True)
        committed, skipped = await recv_type(ws, "committed_transcript")
        timestamps = await recv_json(ws)
    partials = [frame["text"] for frame in skipped]
    assert {frame["message_type"] for frame in skipped} == {"partial_transcript"}
    assert len(partials) >= 3
    assert partials[-1] == TEXT
    assert committed == {"message_type": "committed_transcript", "text": TEXT}
    assert timestamps["message_type"] == "committed_transcript_with_timestamps"
    assert (timestamps["text"], timestamps["language_code"]) == (TEXT, "en")
    assert "".join(entry["text"] for entry in timestamps["words"]) == TEXT
    assert timestamps["words"][0]["start"] == 0.38  # 200 ms lead + 180 ms marker preamble
    (record,) = state.elevenlabs.stt.sessions
    assert record.transcripts[0].text == TEXT
    assert record.transcripts[0].utterance_ids == [1]
    assert record.partials == len(partials)


async def test_commit_waits_stt_final_ms() -> None:
    state = realtime_state()
    state.update_latencies(stt_final_ms=200)
    async with served(state) as base, stt_socket(base) as ws:
        await started(ws)
        sent = time.monotonic()
        await send_audio(ws, SILENCE_100MS, commit=True)
        frame = await recv_json(ws)
        elapsed = time.monotonic() - sent
    assert frame == {"message_type": "committed_transcript", "text": ""}
    assert elapsed >= 0.2


async def test_without_timestamps_only_committed_transcript_is_sent() -> None:
    state = realtime_state()
    async with served(state) as base, stt_socket(base) as ws:
        await started(ws)
        await send_audio(ws, SILENCE_100MS, commit=True)
        await send_audio(ws, b"", commit=True)
        first = await recv_json(ws)
        second = await recv_json(ws)
    assert first == second == {"message_type": "committed_transcript", "text": ""}


async def test_empty_commit_with_timestamps_has_no_words() -> None:
    async with (
        served(realtime_state()) as base,
        stt_socket(base, {**ULAW, "include_timestamps": "true"}) as ws,
    ):
        await started(ws)
        await send_audio(ws, b"", commit=True)
        await recv_type(ws, "committed_transcript")
        timestamps = await recv_json(ws)
    assert timestamps == {
        "message_type": "committed_transcript_with_timestamps",
        "text": "",
        "language_code": "en",
        "words": [],
    }


async def test_entities_frame_follows_the_commit() -> None:
    state = realtime_state()
    text = "my email is jane@mail.test and card 4111 1111 1111 1111"
    state.elevenlabs.stt.script.append(text)
    query = {**ULAW, "entity_detection": ["pii", "pci"]}
    async with served(state) as base, stt_socket(base, query) as ws:
        await started(ws)
        for piece in chunked(LOUD_600MS + SILENCE_100MS * 4, AUDIO_FORMATS["ulaw_8000"]):
            await send_audio(ws, piece)
        await send_audio(ws, b"", commit=True)
        committed, _ = await recv_type(ws, "committed_transcript")
        entities = await recv_json(ws)
    assert committed["text"] == text
    assert entities["message_type"] == "committed_transcript_entities"
    assert entities["text"] == text
    assert [(e["entity_type"], e["text"]) for e in entities["entities"]] == [
        ("email_address", "jane@mail.test"),
        ("credit_card_number", "4111 1111 1111 1111"),
    ]


async def test_script_queue_via_control_route_feeds_markerless_speech() -> None:
    state = realtime_state()
    async with served(state) as base:
        http = base.replace("ws://", "http://", 1)
        async with httpx.AsyncClient(base_url=http) as client:
            posted = await client.post("/sim/elevenlabs/stt/transcripts", json={"texts": ["yes please"]})
        assert posted.status_code == 204
        async with stt_socket(base) as ws:
            await started(ws)
            await send_audio(ws, LOUD_600MS + SILENCE_100MS * 4)
            await send_audio(ws, b"", commit=True)
            committed, _ = await recv_type(ws, "committed_transcript")
    assert committed["text"] == "yes please"
    assert list(state.elevenlabs.stt.script) == []


async def test_vad_commits_without_a_client_commit() -> None:
    state = realtime_state()
    fmt = AUDIO_FORMATS["ulaw_8000"]
    audio = caller_audio(state, "Table for two", fmt.name, tail_ms=800)
    query = {**ULAW, "commit_strategy": "vad", "vad_silence_threshold_secs": "0.5"}
    async with served(state) as base, stt_socket(base, query) as ws:
        await started(ws)
        for piece in chunked(audio, fmt):
            await send_audio(ws, piece)
        committed, _ = await recv_type(ws, "committed_transcript")
    assert committed["text"] == "Table for two"


async def test_previous_text_is_recorded_from_the_first_chunk() -> None:
    state = realtime_state()
    async with served(state) as base, stt_socket(base) as ws:
        await started(ws)
        await send_audio(ws, SILENCE_100MS, previous_text="Hi, this is Karen from Acme.")
        await send_audio(ws, b"", commit=True)
        await recv_json(ws)
    assert state.elevenlabs.stt.sessions[0].previous_text == "Hi, this is Karen from Acme."


# Input errors.


@pytest.mark.parametrize(
    ("message", "error"),
    [
        ("not json", "message must be a JSON object"),
        ("[1, 2]", "message must be a JSON object"),
        ({"audio_base_64": ""}, "unsupported message_type None; expected 'input_audio_chunk'"),
        (
            {"message_type": "transcript_edit", "audio_base_64": ""},
            "unsupported message_type 'transcript_edit'",
        ),
        ({"message_type": "input_audio_chunk"}, "audio_base_64 must be a base64 string"),
        ({"message_type": "input_audio_chunk", "audio_base_64": "@@@"}, "audio_base_64 is not valid base64"),
        (
            {"message_type": "input_audio_chunk", "audio_base_64": "", "commit": "yes"},
            "commit must be a boolean",
        ),
        (
            {"message_type": "input_audio_chunk", "audio_base_64": "", "sample_rate": 16_000},
            "sample_rate 16000 does not match audio_format ulaw_8000",
        ),
        (
            {"message_type": "input_audio_chunk", "audio_base_64": "", "previous_text": 3},
            "previous_text must be a string",
        ),
    ],
)
async def test_input_errors_keep_the_socket_open(message: str | dict[str, object], error: str) -> None:
    async with served(realtime_state()) as base, stt_socket(base) as ws:
        await started(ws)
        if isinstance(message, str):
            await ws.send(message)
        else:
            await send(ws, message)
        frame = await recv_json(ws)
        await send_audio(ws, b"", commit=True)
        after = await recv_json(ws)
    assert frame["message_type"] == "input_error"
    assert frame["error"].startswith(error)
    assert after == {"message_type": "committed_transcript", "text": ""}


async def test_odd_pcm_bytes_are_an_input_error() -> None:
    async with served(realtime_state()) as base, stt_socket(base, PCM16) as ws:
        await started(ws)
        await send_audio(ws, b"\x00\x01\x02")
        frame = await recv_json(ws)
    assert frame == {"message_type": "input_error", "error": "pcm audio must contain an even number of bytes"}


async def test_previous_text_after_the_first_chunk_is_an_input_error() -> None:
    state = realtime_state()
    async with served(state) as base, stt_socket(base) as ws:
        await started(ws)
        await send_audio(ws, SILENCE_100MS)
        await send_audio(ws, SILENCE_100MS, previous_text="late")
        frame = await recv_json(ws)
    assert frame == {
        "message_type": "input_error",
        "error": "previous_text can only be sent with the first audio chunk",
    }
    assert state.elevenlabs.stt.sessions[0].previous_text is None


async def test_chunk_over_one_second_is_dropped_with_its_commit() -> None:
    state = realtime_state()
    async with served(state) as base, stt_socket(base) as ws:
        await started(ws)
        await send_audio(ws, b"\xff" * 8_160, commit=True)
        frame = await recv_json(ws)
        await send_audio(ws, b"\xff" * 8_000, commit=True)
        after = await recv_json(ws)
    assert frame == {"message_type": "chunk_size_exceeded", "error": "Audio chunk exceeds 1000 ms"}
    assert after == {"message_type": "committed_transcript", "text": ""}
    assert state.elevenlabs.stt.sessions[0].audio_ms == 1_000


# Faults.


async def faulted_commit(kind: SttFaultKind) -> tuple[SimState, Closed]:
    """Arm ``kind`` at 0 ms, send 100 ms of silence with a commit; every frame until the socket closed."""
    state = realtime_state()
    state.elevenlabs.stt.arm_fault(kind, after_ms=0, times=1)
    async with served(state) as base, stt_socket(base) as ws:
        await started(ws)
        await send_audio(ws, SILENCE_100MS, commit=True)
        if kind in FATAL_1008 | FATAL_1011:
            return state, await wait_closed(ws)
        frames = (await recv_json(ws), await recv_json(ws))
    return state, Closed(None, None, frames)


@pytest.mark.parametrize("kind", sorted(FATAL_1008 | FATAL_1011))
async def test_fatal_faults_send_their_frame_and_close(kind: SttFaultKind) -> None:
    state, closed = await faulted_commit(kind)
    code = 1008 if kind in FATAL_1008 else 1011
    assert (closed.code, closed.reason) == (code, f"Simulated {kind}")
    assert closed.frames == ({"message_type": kind, "error": f"Simulated {kind}"},)
    record = state.elevenlabs.stt.sessions[0]
    assert (record.end_reason, record.commits) == ("fault", 0)
    assert state.elevenlabs.stt.fault is None


@pytest.mark.parametrize("kind", NON_FATAL)
async def test_non_fatal_faults_leave_the_socket_open(kind: SttFaultKind) -> None:
    _, closed = await faulted_commit(kind)
    frame, after = closed.frames
    assert frame == {"message_type": kind, "error": f"Simulated {kind}"}
    assert after == {"message_type": "committed_transcript", "text": ""}


async def test_disconnect_fault_closes_1011_without_a_frame() -> None:
    state = realtime_state()
    state.elevenlabs.stt.arm_fault("disconnect", after_ms=0, times=1)
    async with served(state) as base, stt_socket(base) as ws:
        await started(ws)
        await send_audio(ws, SILENCE_100MS)
        closed = await wait_closed(ws)
    assert (closed.code, closed.reason, closed.frames) == (1011, "simulated disconnect", ())


async def test_commit_throttled_swallows_one_commit() -> None:
    state = realtime_state()
    fmt = AUDIO_FORMATS["ulaw_8000"]
    audio = caller_audio(state, "Two people", fmt.name)
    state.elevenlabs.stt.arm_fault("commit_throttled", after_ms=0, times=1)
    async with served(state) as base, stt_socket(base) as ws:
        await started(ws)
        for piece in chunked(audio, fmt):
            await send_audio(ws, piece)
        await send_audio(ws, b"", commit=True)
        throttled, _ = await recv_type(ws, "commit_throttled")
        await send_audio(ws, b"", commit=True)
        committed, _ = await recv_type(ws, "committed_transcript")
    assert throttled == {"message_type": "commit_throttled", "error": "Simulated commit_throttled"}
    assert committed["text"] == "Two people"
    assert state.elevenlabs.stt.sessions[0].commits == 1


async def test_fault_waits_for_after_ms_of_audio() -> None:
    state = realtime_state()
    state.elevenlabs.stt.arm_fault("queue_overflow", after_ms=300, times=1)
    async with served(state) as base, stt_socket(base) as ws:
        await started(ws)
        await send_audio(ws, SILENCE_100MS)
        await send_audio(ws, SILENCE_100MS, commit=True)
        before = await recv_json(ws)
        await send_audio(ws, SILENCE_100MS)
        fired = await recv_json(ws)
    assert before == {"message_type": "committed_transcript", "text": ""}
    assert fired["message_type"] == "queue_overflow"


async def test_fault_times_are_consumed_per_firing() -> None:
    state = realtime_state()
    state.elevenlabs.stt.arm_fault("insufficient_audio_activity", after_ms=0, times=2)
    async with served(state) as base, stt_socket(base) as ws:
        await started(ws)
        for _ in range(3):
            await send_audio(ws, SILENCE_100MS)
        await send_audio(ws, b"", commit=True)
        frames = [await recv_json(ws) for _ in range(3)]
    assert [frame["message_type"] for frame in frames] == [
        "insufficient_audio_activity",
        "insufficient_audio_activity",
        "committed_transcript",
    ]
    assert state.elevenlabs.stt.fault is None


def test_every_specs_08_error_type_is_a_fault_kind() -> None:
    assert len(STT_FAULT_KINDS) == 15
    assert set(NON_FATAL) == {
        "queue_overflow",
        "input_error",
        "chunk_size_exceeded",
        "insufficient_audio_activity",
    }


# Reset and records.


async def test_reset_closes_open_sockets_1012() -> None:
    state = realtime_state()
    async with served(state) as base, stt_socket(base) as ws:
        await started(ws)
        record = state.elevenlabs.stt.sessions[0]
        snapshot = state.snapshot()["elevenlabs"]
        assert isinstance(snapshot, dict)
        assert (snapshot["stt_sessions"], snapshot["stt_sockets"]) == (1, 1)
        state.reset()
        closed = await wait_closed(ws)
    assert (closed.code, closed.reason) == (1012, "simulator reset")
    assert record.end_reason == "reset"
    assert state.tasks.pending == 0
    assert state.elevenlabs.stt.sessions == []
    assert state.elevenlabs.stt.sockets == set()


async def test_session_record_after_client_close() -> None:
    state = realtime_state()
    fmt = AUDIO_FORMATS["ulaw_8000"]
    audio = caller_audio(state, "Hello", fmt.name)
    query = {**ULAW, "language_code": "en", "keyterms": ["Acme"], "entity_detection": ["pii"]}
    async with served(state) as base, stt_socket(base, query) as ws:
        await started(ws)
        for piece in chunked(audio, fmt):
            await send_audio(ws, piece)
        await send_audio(ws, b"", commit=True)
        await recv_type(ws, "committed_transcript")
        await recv_type(ws, "committed_transcript_entities")
    record: dict[str, Any] = state.elevenlabs.stt.sessions[0].to_json()
    assert record == {  # the entities frame for "Hello" is sent with an empty list
        "id": record["id"],
        "model_id": "scribe_v2_realtime",
        "audio_format": "ulaw_8000",
        "commit_strategy": "manual",
        "language_code": "en",
        "keyterms": ["Acme"],
        "entity_detection": ["pii"],
        "include_timestamps": False,
        "started_ms": record["started_ms"],
        "previous_text": None,
        "ended_ms": record["ended_ms"],
        "end_reason": "disconnect",
        "audio_ms": len(audio) // 8 // 20 * 20,
        "voiced_ms": record["voiced_ms"],
        "partials": record["partials"],
        "commits": 1,
        "transcripts": [
            {
                "text": "Hello",
                "utterance_ids": [1],
                "language_code": "en",
                "committed_ms": record["transcripts"][0]["committed_ms"],
            }
        ],
    }
    assert record["ended_ms"] >= record["started_ms"]
    assert 500 <= record["voiced_ms"] <= 600  # 180 ms preamble + 5 · 70 ms body
