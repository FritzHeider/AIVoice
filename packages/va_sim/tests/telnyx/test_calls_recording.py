"""Recordings: ``call.recording.saved`` shape, the WAV behind its URL, record-from-answer, single channel."""

from __future__ import annotations

import io
import threading
import wave
from array import array

import httpx
import pytest
from telnyx_helpers import (
    RecordingTransport,
    answered_call,
    at,
    command,
    create_app,
    dial,
    error_of,
    ok,
    payloads,
    ringing_call,
    settle,
    summary,
    telnyx_client,
    telnyx_state,
)
from telnyx_shapes import RecordingSavedPayload, assert_shape

from va_sim.telnyx import call_media
from va_sim.telnyx.call_media import mix_tracks


async def fetch_wav(client: httpx.AsyncClient, url: str) -> wave.Wave_read:
    response = await client.get(url)
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    reader: wave.Wave_read = wave.open(io.BytesIO(response.content), "rb")  # noqa: SIM115 - returned open
    return reader


async def test_record_start_stop_saves_a_dual_channel_wav() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        _, ccid = await answered_call(client, state)
        await ok(client, ccid, "record_start", format="mp3", channels="dual")
        assert error_of(await command(client, ccid, "record_start")) == (422, "10015", None)
        await at(state, 3.5)
        await ok(client, ccid, "record_stop")
        await settle(state)
        [saved] = payloads(transport, "call.recording.saved")
        payload = assert_shape(RecordingSavedPayload, saved)
        assert (payload.channels, payload.format) == ("dual", "mp3")
        assert (payload.recording_started_at, payload.recording_ended_at) == (
            "2026-01-01T00:00:01Z",
            "2026-01-01T00:00:03.500000Z",
        )
        assert payload.recording_urls.mp3 is None
        assert payload.recording_urls.wav == f"http://sim/sim/recordings/{payload.recording_id}.wav"
        assert payload.public_recording_urls == payload.recording_urls
        assert payload.recording_urls.wav is not None
        wav = await fetch_wav(client, payload.recording_urls.wav)
        assert (wav.getnchannels(), wav.getframerate(), wav.getsampwidth()) == (2, 8000, 2)
        assert wav.getnframes() == 2500 * 8
        assert (await summary(client, ccid))["recordings"] == [payload.recording_id]
        assert error_of(await command(client, ccid, "record_stop")) == (422, "10015", None)


async def test_record_from_answer_is_saved_on_hangup_with_captured_audio() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        app = await create_app(client)
        ccid = (await dial(client, app["id"], record="record-from-answer", record_channels="single"))[
            "call_control_id"
        ]
        await at(state, 1.0)
        owner = state.telnyx.call(ccid)
        assert owner is not None
        call = owner[1]
        call.audio.caller.extend(array("h", [1000, 30_000]).tobytes())
        call.audio.agent.extend(array("h", [500, 30_000, 7]).tobytes())
        await at(state, 2.0)
        await ok(client, ccid, "hangup")
        await settle(state)
        [saved] = payloads(transport, "call.recording.saved")
        assert saved["channels"] == "single"
        wav = await fetch_wav(client, saved["recording_urls"]["wav"])
        assert (wav.getnchannels(), wav.getnframes()) == (1, 8000)
        samples = array("h", wav.readframes(3))
        assert list(samples) == [1500, 32_767, 7]


async def test_record_start_before_answer_is_invalid() -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        ccid = await ringing_call(client)
        assert error_of(await command(client, ccid, "record_start")) == (422, "10015", None)


async def test_unknown_recording_is_not_found() -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        response = await client.get("/sim/recordings/missing.wav")
        assert response.status_code == 404


def test_mix_tracks_pads_and_clips() -> None:
    caller = array("h", [-30_000, 2]).tobytes()
    agent = array("h", [-30_000]).tobytes()
    assert list(array("h", mix_tracks(caller, agent))) == [-32_768, 2]


async def test_single_channel_mix_runs_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    mixing_threads: list[threading.Thread] = []

    def recording_mix(caller: bytes, agent: bytes) -> bytes:
        mixing_threads.append(threading.current_thread())
        return mix_tracks(caller, agent)

    monkeypatch.setattr(call_media, "mix_tracks", recording_mix)
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        _, ccid = await answered_call(client, state)
        await ok(client, ccid, "record_start", channels="single")
        await at(state, 2.0)
        await ok(client, ccid, "record_stop")
        await settle(state)
        assert len(payloads(transport, "call.recording.saved")) == 1
    assert len(mixing_threads) == 1
    assert mixing_threads[0] is not threading.main_thread()
