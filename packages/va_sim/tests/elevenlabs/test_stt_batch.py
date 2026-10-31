"""FakeElevenLabs batch STT ``POST /v1/speech-to-text``: a FakeTelnyx recording transcribed end to end, the
response shapes, URL sources, validation, audio errors, faults, and records."""

from __future__ import annotations

import socket
from typing import Any

import httpx
import numpy as np
import pytest
from batch_helpers import (
    BATCH,
    MODEL,
    agent_track,
    caller_track,
    multipart,
    place,
    post_batch,
    served_app,
    wav_file,
    word_entries,
)
from sim_helpers import new_state
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route
from telnyx_helpers import (
    RecordingTransport,
    at,
    create_app,
    dial,
    ok,
    payloads,
    settle,
    telnyx_client,
    telnyx_state,
)
from tts_helpers import VOICE_ID, rest_client

from va_sim.audio.codec import ulaw_decode
from va_sim.elevenlabs import stt_batch
from va_sim.state import SimState

CALLER_1 = "I need to book a table"
AGENT_1 = "Sure, for how many people?"
CALLER_2 = "four of us at seven"
AGENT_2 = "Done. See you tonight!"
# (offset in the recording in ms, channel, text)
PLACEMENTS = [(500, 0, CALLER_1), (3000, 1, AGENT_1), (5500, 0, CALLER_2), (8000, 1, AGENT_2)]
MARKER_S = 0.18


def texts_of(words: list[dict[str, Any]]) -> str:
    return " ".join(str(entry["text"]) for entry in word_entries(words))


async def spoken(client: httpx.AsyncClient, text: str) -> np.ndarray:
    """``text`` synthesized through REST TTS as μ-law (recorded in ``tts_requests``), decoded."""
    response = await client.post(
        f"/v1/text-to-speech/{VOICE_ID}", params={"output_format": "ulaw_8000"}, json={"text": text}
    )
    assert response.status_code == 200
    return ulaw_decode(response.content)


async def recorded_call(
    state: SimState, transport: RecordingTransport, elevenlabs: httpx.AsyncClient
) -> bytes:
    """A dual-channel FakeTelnyx recording holding the four placements; returns its WAV."""
    caller = [(offset, caller_track(state, text)) for offset, channel, text in PLACEMENTS if channel == 0]
    agent = [(offset, await spoken(elevenlabs, text)) for offset, channel, text in PLACEMENTS if channel == 1]
    async with telnyx_client(state) as telnyx:
        app = await create_app(telnyx)
        call = await dial(telnyx, app["id"], record="record-from-answer", record_channels="dual")
        # The REST syntheses above cost first-audio latency, so the call answers a little after 1 s.
        await at(state, 2.0)
        owner = state.telnyx.call(call["call_control_id"])
        assert owner is not None
        owner[1].audio.caller.extend(place(caller, 11_000).astype("<i2").tobytes())
        owner[1].audio.agent.extend(place(agent, 11_000).astype("<i2").tobytes())
        await at(state, 13.0)
        await ok(telnyx, call["call_control_id"], "hangup")
        await settle(state)
    [saved] = payloads(transport, "call.recording.saved")
    return state.recordings[saved["recording_id"]]


async def test_a_dual_channel_call_recording_transcribes_both_sides_in_order() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with rest_client(state) as client:
        wav = await recorded_call(state, transport, client)
        response = await post_batch(
            client,
            {
                "use_multi_channel": "true",
                "multichannel_output_style": "combined",
                "timestamps_granularity": "word",
            },
            wav,
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "language_code",
        "language_probability",
        "text",
        "words",
        "transcription_id",
        "audio_duration_secs",
    }
    words = word_entries(body["words"])
    assert body["text"] == texts_of(body["words"]) == " ".join(text for _, _, text in PLACEMENTS)
    assert [entry["start"] for entry in body["words"]] == sorted(entry["start"] for entry in body["words"])
    assert {(entry["channel_index"], entry["speaker_id"]) for entry in words} == {
        (0, "speaker_0"),
        (1, "speaker_1"),
    }
    for offset, channel, text in PLACEMENTS:
        first = next(
            entry for entry in words if entry["text"] == text.split()[0] and entry["channel_index"] == channel
        )
        expected = offset / 1000 + (MARKER_S if channel == 0 else 0.0)
        assert abs(first["start"] - expected) <= 0.07, (text, first)
    assert (body["language_code"], body["language_probability"]) == ("en", 1.0)
    assert len(body["transcription_id"]) == 32
    assert int(body["transcription_id"], 16) >= 0
    assert 11.0 < body["audio_duration_secs"] < 12.0


def two_sided(state: SimState) -> bytes:
    caller = place([(200, caller_track(state, "my email is jo@acme.io", language="es"))], 4000)
    agent = place([(2500, agent_track("Got it."))], 4000)
    return wav_file([caller, agent])


async def tts(client: httpx.AsyncClient, text: str) -> None:
    assert (await client.post(f"/v1/text-to-speech/{VOICE_ID}", json={"text": text})).status_code == 200


async def test_separate_lists_one_transcript_per_channel() -> None:
    state = new_state()
    async with rest_client(state) as client:
        await tts(client, "Got it.")
        response = await post_batch(client, {"use_multi_channel": "true"}, two_sided(state))
    body = response.json()
    assert set(body) == {"transcripts", "transcription_id", "audio_duration_secs"}
    left, right = body["transcripts"]
    assert set(left) == {"language_code", "language_probability", "text", "words", "channel_index"}
    assert (left["channel_index"], left["text"], left["language_code"]) == (0, "my email is jo@acme.io", "es")
    assert (right["channel_index"], right["text"], right["language_code"]) == (1, "Got it.", "en")
    assert {entry["speaker_id"] for entry in right["words"]} == {"speaker_1"}
    assert all(entry["channel_index"] == 0 for entry in left["words"])
    spacing = left["words"][1]
    assert spacing == {
        "text": " ",
        "start": left["words"][0]["end"],
        "end": left["words"][2]["start"],
        "type": "spacing",
        "speaker_id": "speaker_0",
        "logprob": 0.0,
        "channel_index": 0,
    }


async def test_without_multi_channel_the_mono_shape_and_diarized_speakers() -> None:
    state = new_state()
    caller = place([(200, caller_track(state, "hello there"))], 3000)
    agent = place([(1800, agent_track("Welcome"))], 3000)
    mono = wav_file([place([(0, caller), (0, agent)], 3000)])
    async with rest_client(state) as client:
        await tts(client, "Welcome")
        plain = (await post_batch(client, {"language_code": "de"}, mono)).json()
        diarized = (await post_batch(client, {"diarize": "TRUE"}, mono)).json()
    assert plain["text"] == "hello there Welcome"
    assert plain["language_code"] == "en"
    assert all("channel_index" not in entry for entry in plain["words"])
    assert {entry["speaker_id"] for entry in plain["words"]} == {"speaker_0"}
    speakers = [(entry["text"], entry["speaker_id"]) for entry in word_entries(diarized["words"])]
    assert speakers == [("hello", "speaker_0"), ("there", "speaker_0"), ("Welcome", "speaker_1")]


async def test_language_falls_back_to_the_request_then_en() -> None:
    agent_only = wav_file([place([(200, agent_track("Hi"))], 1000)])
    async with rest_client(new_state()) as client:
        await tts(client, "Hi")
        requested = (await post_batch(client, {"language_code": "pt"}, agent_only)).json()
        default = (await post_batch(client, {}, agent_only)).json()
    assert (requested["language_code"], default["language_code"]) == ("pt", "en")


async def test_character_granularity_and_none() -> None:
    state = new_state()
    data = wav_file([place([(100, agent_track("ok go"))], 1000)])
    async with rest_client(state) as client:
        await tts(client, "ok go")
        chars = (await post_batch(client, {"timestamps_granularity": "character"}, data)).json()
        none = (await post_batch(client, {"timestamps_granularity": "none"}, data)).json()
    first = chars["words"][0]
    assert first["characters"] == [
        {"text": "o", "start": 0.1, "end": 0.16},
        {"text": "k", "start": 0.16, "end": 0.22},
    ]
    assert "characters" not in chars["words"][1]
    assert (none["words"], none["text"]) == ([], "ok go")


async def test_entity_detection_on_single_and_separate() -> None:
    state = new_state()
    data = two_sided(state)
    async with rest_client(state) as client:
        await tts(client, "Got it.")
        separate = await post_batch(
            client, {"use_multi_channel": "true", "entity_detection": ["pii", "pci"]}, data
        )
        single = await post_batch(client, {"entity_detection": "all"}, data)
    left, right = separate.json()["transcripts"]
    email = {"text": "jo@acme.io", "entity_type": "email_address", "start_char": 12, "end_char": 22}
    assert (left["entities"], right["entities"]) == ([email], [])
    assert single.json()["entities"] == [email]


async def test_a_stored_recording_url_is_read_in_process_whatever_the_host() -> None:
    state = new_state()
    state.store_recording("rec-1", two_sided(state))
    async with rest_client(state) as client:
        await tts(client, "Got it.")
        response = await post_batch(
            client,
            {
                "cloud_storage_url": "https://storage.invalid/sim/recordings/rec-1.wav",
                "use_multi_channel": "true",
            },
        )
        records = (await client.get("/sim/elevenlabs/stt/batch")).json()["requests"]
    assert response.status_code == 200
    assert [t["text"] for t in response.json()["transcripts"]] == ["my email is jo@acme.io", "Got it."]
    assert (records[0]["source"], records[0]["source_url"], records[0]["channels"]) == (
        "url",
        "https://storage.invalid/sim/recordings/rec-1.wav",
        2,
    )


def audio_server(wav: bytes) -> Starlette:
    async def audio(_request: Request) -> Response:
        return Response(wav, media_type="audio/wav")

    async def moved(_request: Request) -> Response:
        return RedirectResponse("/audio.wav")

    async def missing(_request: Request) -> Response:
        return Response(status_code=404)

    return Starlette(
        routes=[
            Route("/audio.wav", audio),
            Route("/moved.wav", moved),
            Route("/sim/recordings/unknown.wav", missing),
        ]
    )


def closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


async def test_source_url_is_fetched_over_http() -> None:
    state = new_state()
    data = wav_file([place([(100, agent_track("fetched"))], 1000)])
    async with served_app(audio_server(data)) as base, rest_client(state) as client:
        await tts(client, "fetched")
        fetched = await post_batch(client, {"source_url": f"{base}/audio.wav"})
        redirected = await post_batch(client, {"source_url": f"{base}/moved.wav"})
        missing = await post_batch(client, {"source_url": f"{base}/sim/recordings/unknown.wav"})
        refused = await post_batch(client, {"source_url": f"http://127.0.0.1:{closed_port()}/a.wav"})
    assert (fetched.status_code, fetched.json()["text"]) == (200, "fetched")
    for response, why in ((redirected, "HTTP 307"), (missing, "HTTP 404"), (refused, "ConnectError")):
        assert response.status_code == 400
        assert response.json()["detail"]["status"] == "invalid_source_url"
        assert response.json()["detail"]["message"].endswith(f"({why})")
    assert len(state.elevenlabs.stt.batch) == 1


async def test_a_fetch_over_the_cap_is_too_large(monkeypatch: pytest.MonkeyPatch) -> None:
    data = wav_file([np.zeros(8000, dtype=np.int16)])
    monkeypatch.setattr(stt_batch, "MAX_UPLOAD_BYTES", len(data) - 1)
    async with served_app(audio_server(data)) as base, rest_client(new_state()) as client:
        fetched = await post_batch(client, {"source_url": f"{base}/audio.wav"})
        uploaded = await post_batch(client, {}, data)
    for response in (fetched, uploaded):
        assert response.status_code == 400
        assert response.json()["detail"]["status"] == "file_too_large"


SILENT = wav_file([np.zeros(8000, dtype=np.int16)])


@pytest.mark.parametrize(
    ("fields", "file", "loc"),
    [
        ({"model_id": ""}, SILENT, ["body", "model_id"]),
        ({}, None, ["body"]),
        ({"source_url": "http://a.test/x.wav"}, SILENT, ["body"]),
        ({"source_url": "ftp://a.test/x.wav"}, None, ["body", "source_url"]),
        ({"cloud_storage_url": "not a url"}, None, ["body", "cloud_storage_url"]),
        ({"timestamps_granularity": "sentence"}, SILENT, ["body", "timestamps_granularity"]),
        ({"diarize": "yes"}, SILENT, ["body", "diarize"]),
        ({"num_speakers": "0"}, SILENT, ["body", "num_speakers"]),
        ({"num_speakers": "33"}, SILENT, ["body", "num_speakers"]),
        ({"language_code": "EN"}, SILENT, ["body", "language_code"]),
        ({"keyterms": "x" * 50}, SILENT, ["body", "keyterms", 0]),
        ({"keyterms": ["ok", "one two three four five six"]}, SILENT, ["body", "keyterms", 1]),
        ({"keyterms": "a<b"}, SILENT, ["body", "keyterms", 0]),
        ({"entity_detection": "names"}, SILENT, ["body", "entity_detection", 0]),
        ({"file_format": "mp3"}, SILENT, ["body", "file_format"]),
        ({"multichannel_output_style": "combined", "timestamps_granularity": "none"}, SILENT, ["body"]),
        ({"multichannel_output_style": "combined", "entity_detection": "pii"}, SILENT, ["body"]),
        ({"file": "text, not an upload"}, None, ["body", "file"]),
    ],
)
async def test_invalid_fields_are_422(
    fields: dict[str, str | list[str]], file: bytes | None, loc: list[object]
) -> None:
    async with rest_client(new_state()) as client:
        response = await post_batch(client, fields, file)
    assert response.status_code == 422
    assert loc in [item["loc"] for item in response.json()["detail"]]


async def test_missing_model_bad_logging_and_non_multipart_are_422() -> None:
    async with rest_client(new_state()) as client:
        missing = await client.post(BATCH, files=multipart({}, SILENT))
        logging = await post_batch(client, {}, SILENT, params={"enable_logging": "maybe"})
        urlencoded = await client.post(BATCH, data={"model_id": MODEL})
        broken = await client.post(
            BATCH, content=b"--x\r\nbroken", headers={"content-type": "multipart/form-data; boundary=x"}
        )
        two_files = await client.post(
            BATCH,
            files=[("model_id", (None, MODEL)), ("file", ("a.wav", SILENT)), ("other", ("b.wav", SILENT))],
        )
    assert missing.json()["detail"][0]["loc"] == ["body", "model_id"]
    assert missing.json()["detail"][0]["type"] == "missing"
    assert logging.json()["detail"] == [
        {"loc": ["query", "enable_logging"], "msg": "must be true or false", "type": "bool_parsing"}
    ]
    for response in (urlencoded, two_files):
        assert response.status_code == 422, response.text
        assert response.json()["detail"][0]["loc"] == ["body"]
    # python-multipart reads a truncated body as an empty form.
    assert broken.json()["detail"][0]["loc"] == ["body", "model_id"]


async def test_auth_is_checked_first() -> None:
    async with rest_client(new_state(), key=None) as client:
        missing = await client.post(BATCH, data={"model_id": MODEL})
        invalid = await client.post(BATCH, headers={"xi-api-key": ""}, data={})
    assert (missing.status_code, missing.json()["detail"]["status"]) == (401, "needs_authorization")
    state = new_state()
    state.elevenlabs.api_keys = ("other",)
    async with rest_client(state) as client:
        denied = await client.post(BATCH, data={"model_id": MODEL})
    assert (denied.status_code, denied.json()["detail"]["status"]) == (401, "invalid_api_key")
    assert invalid.status_code == 401


@pytest.mark.parametrize(
    ("fields", "data", "status"),
    [
        ({}, b"not audio at all", "invalid_audio"),
        ({"use_multi_channel": "true"}, wav_file([np.zeros(800, dtype=np.int16)] * 6), "too_many_channels"),
        ({}, wav_file([np.zeros(400, dtype=np.int16)]), "audio_too_short"),
        ({"file_format": "pcm_s16le_16"}, b"\x00" * 1001, "invalid_audio"),
    ],
)
async def test_unusable_audio_is_400(fields: dict[str, str], data: bytes, status: str) -> None:
    state = new_state()
    async with rest_client(state) as client:
        response = await post_batch(client, fields, data)
    assert response.status_code == 400
    assert response.json()["detail"]["status"] == status
    assert state.elevenlabs.stt.batch == []


async def test_raw_pcm_upload_transcribes_at_16_khz() -> None:
    samples = place([(100, agent_track("raw", 16000))], 1000, 16000)
    async with rest_client(new_state()) as client:
        await tts(client, "raw")
        response = await post_batch(client, {"file_format": "pcm_s16le_16"}, samples.astype("<i2").tobytes())
    assert (response.json()["text"], response.json()["audio_duration_secs"]) == ("raw", 1.0)


async def test_faults_fire_after_auth_and_are_consumed() -> None:
    state = new_state()
    async with rest_client(state) as client:
        await client.put("/sim/elevenlabs/stt/batch/fault", json={"kind": "error", "times": 2})
        first = await client.post(BATCH, data={})
        second = await post_batch(client, {}, SILENT)
        third = await post_batch(client, {}, SILENT)
        await client.put("/sim/elevenlabs/stt/batch/fault", json={"kind": "rate_limited"})
        limited = await post_batch(client, {}, SILENT)
    assert (first.status_code, second.status_code, third.status_code) == (500, 500, 200)
    assert first.json() == {"detail": {"status": "internal_error", "message": "Simulated batch STT failure"}}
    assert limited.status_code == 429
    assert limited.json()["detail"]["status"] == "too_many_concurrent_requests"
    assert state.elevenlabs.stt.batch_fault is None
    assert len(state.elevenlabs.stt.batch) == 1


async def test_a_delay_fault_waits_on_the_sim_clock() -> None:
    state = new_state()
    async with rest_client(state) as client:
        await client.put("/sim/elevenlabs/stt/batch/fault", json={"kind": "delay", "delay_ms": 2500})
        before = state.clock.virtual_ms()
        response = await post_batch(client, {}, SILENT)
    assert response.status_code == 200
    assert state.clock.virtual_ms() - before >= 2500
    assert response.json()["text"] == ""


async def test_records_list_parameters_and_reset_clears_them() -> None:
    state = new_state()
    async with rest_client(state) as client:
        response = await post_batch(
            client,
            {
                "keyterms": ["table", "seven pm"],
                "num_speakers": "2",
                "tag_audio_events": "false",
                "unknown": "x",
            },
            SILENT,
            params={"enable_logging": "false"},
        )
        await client.put("/sim/elevenlabs/stt/batch/fault", json={"kind": "error"})
        [record] = (await client.get("/sim/elevenlabs/stt/batch")).json()["requests"]
        assert state.snapshot()["elevenlabs"]["stt_batch"] == 1  # type: ignore[index, call-overload]
        assert (await client.post("/sim/reset")).status_code == 204
        assert (await client.get("/sim/elevenlabs/stt/batch")).json() == {"requests": []}
    assert record == {
        "transcription_id": response.json()["transcription_id"],
        "model_id": MODEL,
        "source": "file",
        "source_url": None,
        "file_format": "other",
        "channels": 1,
        "use_multi_channel": False,
        "multichannel_output_style": "separate",
        "diarize": False,
        "num_speakers": 2,
        "timestamps_granularity": "word",
        "language_code": None,
        "keyterms": ["table", "seven pm"],
        "entity_detection": [],
        "enable_logging": False,
        "audio_duration_secs": 1.0,
        "received_ms": record["received_ms"],
    }
    assert state.elevenlabs.stt.batch_fault is None
