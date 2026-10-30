"""FakeElevenLabs REST synthesis: convert, stream, with-timestamps, auth/validation errors, faults."""

from __future__ import annotations

import base64

import pytest
from sim_helpers import new_state
from tts_helpers import VOICE_ID, rest_client

from va_sim.elevenlabs.tts_audio import absolute_alignment, chunks, parse_output_format, synthesize

CONVERT = f"/v1/text-to-speech/{VOICE_ID}"
TEXT = "Thanks for calling. "


@pytest.mark.parametrize(
    ("output_format", "content_type"),
    [("ulaw_8000", "audio/basic"), ("pcm_16000", "audio/pcm"), ("mp3_44100_128", "audio/mpeg")],
)
async def test_convert_returns_the_generator_audio(output_format: str, content_type: str) -> None:
    state = new_state()
    async with rest_client(state) as client:
        response = await client.post(CONVERT, params={"output_format": output_format}, json={"text": TEXT})
    assert response.status_code == 200
    assert response.headers["content-type"] == content_type
    assert response.content == synthesize(TEXT, parse_output_format(output_format))
    assert response.headers["x-character-count"] == str(len(TEXT))
    assert response.headers["request-id"] == "ttsreq_1"


async def test_convert_defaults_to_mp3_and_waits_ttfa() -> None:
    state = new_state()
    async with rest_client(state) as client:
        response = await client.post(CONVERT, json={"text": TEXT, "unknown": 1})
    assert response.headers["content-type"] == "audio/mpeg"
    assert state.clock.virtual_ms() >= state.latencies.tts_ttfa_ms
    record = state.elevenlabs.tts_requests[0].to_json()
    assert record["transport"] == "rest"
    assert record["model_id"] == "eleven_multilingual_v2"
    assert (record["context_id"], record["end_reason"], record["interrupted"]) == (None, "completed", False)
    assert (record["text"], record["generated_text"], record["characters"]) == (TEXT, TEXT, len(TEXT))


async def test_stream_yields_200_ms_pieces_matching_convert() -> None:
    state = new_state()
    fmt = parse_output_format("ulaw_8000")
    async with (
        rest_client(state) as client,
        client.stream(
            "POST", f"{CONVERT}/stream", params={"output_format": "ulaw_8000"}, json={"text": TEXT}
        ) as response,
    ):
        assert response.status_code == 200
        body = b"".join([part async for part in response.aiter_raw()])
    assert body == synthesize(TEXT, fmt) == b"".join(piece.audio for piece in chunks(TEXT, fmt))
    assert state.elevenlabs.tts_requests[0].end_reason == "completed"


async def test_with_timestamps_returns_absolute_alignment() -> None:
    state = new_state()
    async with rest_client(state) as client:
        response = await client.post(
            f"{CONVERT}/with-timestamps", params={"output_format": "pcm_16000"}, json={"text": "Hi!"}
        )
    body = response.json()
    assert set(body) == {"audio_base64", "alignment", "normalized_alignment"}
    assert base64.b64decode(body["audio_base64"]) == synthesize("Hi!", parse_output_format("pcm_16000"))
    assert body["alignment"] == body["normalized_alignment"] == absolute_alignment("Hi!")
    assert body["alignment"]["character_end_times_seconds"][-1] == 0.18


async def test_missing_key_is_401_needs_authorization() -> None:
    async with rest_client(new_state(), key=None) as client:
        response = await client.post(CONVERT, json={"text": TEXT})
    assert response.status_code == 401
    assert response.json() == {
        "detail": {
            "status": "needs_authorization",
            "message": "Neither authorization header nor xi-api-key received, please provide one.",
        }
    }


async def test_rejected_key_is_401_invalid_api_key() -> None:
    state = new_state()
    state.elevenlabs.api_keys = ("only-this",)
    async with rest_client(state, key="another") as client:
        response = await client.post(CONVERT, json={"text": TEXT})
    assert response.status_code == 401
    assert response.json() == {"detail": {"status": "invalid_api_key", "message": "Invalid API key"}}


async def test_empty_text_is_422() -> None:
    async with rest_client(new_state()) as client:
        response = await client.post(CONVERT, json={"text": ""})
    assert response.status_code == 422
    (error,) = response.json()["detail"]
    assert error["loc"] == ["body", "text"]
    assert error["type"] == "string_too_short"


async def test_bad_output_format_is_422() -> None:
    async with rest_client(new_state()) as client:
        response = await client.post(CONVERT, params={"output_format": "wav"}, json={"text": TEXT})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "output_format"]


async def test_invalid_json_is_422() -> None:
    async with rest_client(new_state()) as client:
        response = await client.post(CONVERT, content=b"{nope", headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body"]
    assert response.json()["detail"][0]["type"] == "json_invalid"


async def test_fault_error_is_500_and_consumed() -> None:
    state = new_state()
    state.elevenlabs.arm_fault("error", after_chunks=0, delay_ms=0, times=1)
    async with rest_client(state) as client:
        failed = await client.post(CONVERT, json={"text": TEXT})
        recovered = await client.post(CONVERT, json={"text": TEXT})
    assert failed.status_code == 500
    assert failed.json() == {"detail": {"status": "internal_error", "message": "Simulated TTS failure"}}
    assert recovered.status_code == 200
    first, second = state.elevenlabs.tts_requests
    assert (first.end_reason, first.interrupted) == ("fault", True)
    assert second.end_reason == "completed"


async def test_fault_delay_adds_latency_and_disconnect_does_not_apply() -> None:
    state = new_state()
    state.elevenlabs.arm_fault("delay", after_chunks=0, delay_ms=500, times=1)
    async with rest_client(state) as client:
        await client.post(CONVERT, json={"text": TEXT})
        assert state.clock.virtual_ms() >= 500 + state.latencies.tts_ttfa_ms
        state.elevenlabs.arm_fault("disconnect", after_chunks=0, delay_ms=0, times=1)
        response = await client.post(CONVERT, json={"text": TEXT})
    assert response.status_code == 200
    assert state.elevenlabs.fault is not None
