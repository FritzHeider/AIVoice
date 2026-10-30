"""FakeTelnyx media WebSocket client against a test agent: handshake signature, framing, caller audio, agent
playout with marks and clear, errors, DTMF, injection, jitter, lifecycle, recordings, and the timeline."""

from __future__ import annotations

import base64
import io
import socket
import time
import wave
from typing import Any

import httpx
import numpy as np
from media_agent import MediaAgent
from nacl.signing import VerifyKey
from numpy.typing import NDArray
from sim_helpers import LATENCIES
from telnyx_helpers import (
    MESSAGING_URL,
    VOICE_URL,
    RecordingTransport,
    answered_call,
    at,
    command,
    error_of,
    events,
    ok,
    payloads,
    settle,
    summary,
    telnyx_client,
    telnyx_state,
)

from va_sim.audio.codec import l16be_to_pcm, ulaw_decode, ulaw_encode
from va_sim.audio.marker import decode_marker
from va_sim.clock import DEFAULT_START, RealtimeClock
from va_sim.settings import WebhookDefaults
from va_sim.state import SimState

AUTH_TOKEN = "stream-token-1"  # noqa: S105 - test value for the Bearer header


def tone(ms: int, amplitude: int = 8000) -> NDArray[np.int16]:
    """A 440 Hz tone at 8 kHz."""
    t = np.arange(ms * 8) / 8000
    return (amplitude * np.sin(2 * np.pi * 440 * t)).astype(np.int16)


def agent_media(audio: bytes) -> dict[str, Any]:
    return {"event": "media", "media": {"payload": base64.b64encode(audio).decode()}}


def payload_of(frame: dict[str, Any]) -> bytes:
    return base64.b64decode(frame["media"]["payload"])


def started(agent: MediaAgent) -> bool:
    return len(agent.frames) >= 2


async def streaming(client: httpx.AsyncClient, state: SimState, agent: MediaAgent, **dial: Any) -> str:
    """An outgoing call answered at 1 s that streams to ``agent``; returns once ``start`` has arrived."""
    agent.clock = state.clock
    _, ccid = await answered_call(client, state, stream_url=agent.url, **dial)
    await agent.wait_for(started)
    return ccid


def wav_channels(wav: bytes) -> list[NDArray[np.int16]]:
    with wave.open(io.BytesIO(wav)) as reader:
        channels = reader.getnchannels()
        samples = np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2")
    return [samples[index::channels] for index in range(channels)]


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


async def test_handshake_signature_and_first_frames(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        ccid = await streaming(
            client, state, media_agent, stream_auth_token=AUTH_TOKEN, client_state="c3RhdGU="
        )
        await media_agent.wait_media(5)
        headers = media_agent.headers
        assert headers is not None
        assert media_agent.request_path == "/ws/telnyx?call_id=1"
        timestamp = headers["telnyx-timestamp"]
        assert int(timestamp) == int(DEFAULT_START.timestamp()) + 1
        signature = base64.b64decode(headers["telnyx-signature-ed25519"])
        VerifyKey(base64.b64decode(state.signing_key.public_key_b64)).verify(
            f"{timestamp}|{media_agent.url}".encode(), signature
        )
        assert headers["authorization"] == f"Bearer {AUTH_TOKEN}"
        connected, start = media_agent.frames[:2]
        assert connected == {"event": "connected", "version": "1.0.0"}
        stream_id = (await summary(client, ccid))["stream"]["stream_id"]
        call = state.telnyx.call(ccid)
        assert call is not None
        assert start == {
            "event": "start",
            "sequence_number": "1",
            "start": {
                "user_id": start["start"]["user_id"],
                "call_control_id": ccid,
                "call_session_id": call[1].call_session_id,
                "from": "+14155552000",
                "to": "+14155550101",
                "tags": [],
                "client_state": "c3RhdGU=",
                "media_format": {"encoding": "PCMU", "sample_rate": 8000, "channels": 1},
            },
            "stream_id": stream_id,
        }
        frames = media_agent.media()[:5]
        assert [f["media"]["chunk"] for f in frames] == ["1", "2", "3", "4", "5"]
        assert [f["media"]["timestamp"] for f in frames] == ["0", "20", "40", "60", "80"]
        assert [f["sequence_number"] for f in frames] == ["2", "3", "4", "5", "6"]
        assert {f["media"]["track"] for f in frames} == {"inbound"}
        assert {f["stream_id"] for f in frames} == {stream_id}
        assert {payload_of(f) for f in frames} == {b"\xff" * 160}
        assert media_agent.arrivals[2] >= 1000
        detail = (await summary(client, ccid))["stream"]
        assert detail["connected"] is True
        assert detail["frames_sent"] >= 5


async def test_no_authorization_header_without_token(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        await streaming(client, state, media_agent)
        assert media_agent.headers is not None
        assert "authorization" not in media_agent.headers


async def test_l16_wideband_stream(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        await streaming(client, state, media_agent, stream_codec="L16")
        await media_agent.wait_media(2)
        media_format = media_agent.frames[1]["start"]["media_format"]
        assert media_format == {"encoding": "L16", "sample_rate": 16000, "channels": 1}
        assert payload_of(media_agent.media()[0]) == bytes(640)


async def test_say_streams_marker_audio(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        ccid = await streaming(client, state, media_agent)
        response = await client.post(f"/sim/telnyx/calls/{ccid}/say", json={"text": "hello there"})
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["duration_ms"] == 180 + len("hello there") * 70
        sent_before = len(media_agent.media())
        await media_agent.wait_media(sent_before + body["duration_ms"] // 20 + 2)
        audio = b"".join(payload_of(f) for f in media_agent.media()[sent_before:])
        assert decode_marker(ulaw_decode(audio), 8000) == body["utterance_id"]
        assert state.utterances[body["utterance_id"]].text == "hello there"


async def test_say_on_wideband_stream(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        ccid = await streaming(client, state, media_agent, stream_codec="L16")
        response = await client.post(f"/sim/telnyx/calls/{ccid}/say", json={"text": "wide", "language": "de"})
        assert response.status_code == 202, response.text
        body = response.json()
        sent_before = len(media_agent.media())
        await media_agent.wait_media(sent_before + body["duration_ms"] // 20 + 2)
        audio = b"".join(payload_of(f) for f in media_agent.media()[sent_before:])
        assert decode_marker(l16be_to_pcm(audio), 16000) == body["utterance_id"]
        assert state.utterances[body["utterance_id"]].language == "de"


async def test_say_on_narrowband_l16_stream(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        ccid = await streaming(
            client, state, media_agent, stream_codec="L16", stream_bidirectional_sampling_rate=8000
        )
        response = await client.post(f"/sim/telnyx/calls/{ccid}/say", json={"text": "narrow"})
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["duration_ms"] == 180 + len("narrow") * 70
        sent_before = len(media_agent.media())
        await media_agent.wait_media(sent_before + body["duration_ms"] // 20 + 2)
        audio = b"".join(payload_of(f) for f in media_agent.media()[sent_before:])
        assert decode_marker(l16be_to_pcm(audio), 8000) == body["utterance_id"]


async def test_agent_audio_plays_out_and_mark_is_echoed_after_it(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        ccid = await streaming(client, state, media_agent)
        audio = ulaw_encode(tone(60))
        sent_at = len(media_agent.frames)
        await media_agent.send(agent_media(audio))
        await media_agent.send({"event": "mark", "mark": {"name": "seg:0:0"}})
        await media_agent.wait_for(lambda agent: bool(agent.of("mark")))
        [mark] = media_agent.of("mark")
        position = media_agent.frames.index(mark)
        between = [f for f in media_agent.frames[sent_at:position] if f["event"] == "media"]
        assert len(between) >= 2
        following = media_agent.frames[position + 1] if position + 1 < len(media_agent.frames) else None
        stream_id = (await summary(client, ccid))["stream"]["stream_id"]
        assert mark == {
            "event": "mark",
            "stream_id": stream_id,
            "sequence_number": mark["sequence_number"],
            "mark": {"name": "seg:0:0"},
        }
        if following is not None:
            assert int(following["sequence_number"]) == int(mark["sequence_number"]) + 1
        wav = (await client.get(f"/sim/telnyx/calls/{ccid}/agent_audio.wav")).content
        [agent_track] = wav_channels(wav)
        assert ulaw_decode(audio).astype("<i2").tobytes() in agent_track.tobytes()
        detail = (await summary(client, ccid))["stream"]
        assert detail["received"] == {"media": 1, "mark": 1}
        assert detail["marks_echoed"] == 1


async def test_mark_with_nothing_queued_is_echoed_before_the_next_media_frame(
    media_agent: MediaAgent,
) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        await streaming(client, state, media_agent)
        await media_agent.send({"event": "mark", "mark": {"name": "empty"}})
        await media_agent.wait_for(
            lambda agent: bool(agent.of("mark")) and agent.frames[-1]["event"] == "media"
        )
        [mark] = media_agent.of("mark")
        following = media_agent.frames[media_agent.frames.index(mark) + 1]
        assert following["event"] == "media"
        assert int(following["sequence_number"]) == int(mark["sequence_number"]) + 1


async def test_clear_drops_queued_audio_and_echoes_pending_marks(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        ccid = await streaming(client, state, media_agent)
        sent_at = len(media_agent.frames)
        await media_agent.send(agent_media(ulaw_encode(tone(1000))))
        await media_agent.send({"event": "mark", "mark": {"name": "a"}})
        await media_agent.send({"event": "mark", "mark": {"name": "b"}})
        await media_agent.send({"event": "clear"})
        await media_agent.wait_for(lambda agent: len(agent.of("mark")) == 2)
        first, second = media_agent.of("mark")
        assert (first["mark"]["name"], second["mark"]["name"]) == ("a", "b")
        position = media_agent.frames.index(first)
        assert media_agent.frames[position + 1] == second
        assert len([f for f in media_agent.frames[sent_at:position] if f["event"] == "media"]) < 45
        await media_agent.wait_media(len(media_agent.media()) + 60)
        [agent_track] = wav_channels((await client.get(f"/sim/telnyx/calls/{ccid}/agent_audio.wav")).content)
        assert np.count_nonzero(agent_track) < 8000


async def test_malformed_agent_frames_get_error_frames(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        ccid = await streaming(client, state, media_agent)
        for raw in (
            b"binary",
            "not json",
            "[1]",
            '{"event": "nope"}',
            '{"event": "mark", "mark": {}}',
            '{"event": "media", "media": {}}',
            '{"event": "media", "media": {"payload": "%%%"}}',
            '{"event": "media", "media": {"payload": ""}}',
        ):
            await media_agent.send_raw(raw)
        await media_agent.wait_for(lambda agent: len(agent.of("error")) == 8)
        errors = media_agent.of("error")
        assert [e["payload"]["code"] for e in errors] == [100003] * 5 + [100004] * 3
        assert {e["payload"]["title"] for e in errors} == {"malformed_frame", "invalid_media"}
        assert all("sequence_number" not in e and e["stream_id"] for e in errors)
        count = len(media_agent.media())
        await media_agent.wait_media(count + 3)
        assert not media_agent.closed
        assert (await summary(client, ccid))["stream"]["received"] == {"invalid": 8}


async def test_dtmf_control_sends_one_event_per_digit(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        ccid = await streaming(client, state, media_agent)
        response = await client.post(f"/sim/telnyx/calls/{ccid}/dtmf", json={"digits": "12#"})
        assert response.status_code == 202, response.text
        await media_agent.wait_for(lambda agent: len(agent.of("dtmf")) == 3)
        dtmf = media_agent.of("dtmf")
        assert [d["dtmf"]["digit"] for d in dtmf] == ["1", "2", "#"]
        assert all(d["occurred_at"].startswith("2026-01-01T00:00:") for d in dtmf)
        positions = [media_agent.frames.index(d) for d in dtmf]
        assert all(media_agent.frames[p + 1]["event"] == "media" for p in positions[:2])
        caller_dtmf = (await summary(client, ccid))["caller_dtmf"]
        assert [(d["digit"], d["at"]) for d in caller_dtmf] == [
            (d["dtmf"]["digit"], d["occurred_at"]) for d in dtmf
        ]
        bad = await client.post(f"/sim/telnyx/calls/{ccid}/dtmf", json={"digits": "12x"})
        assert bad.status_code == 422


async def test_inject_sends_the_frame_verbatim(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        ccid = await streaming(client, state, media_agent)
        frame = {"event": "media", "sequence_number": "999", "media": {"chunk": "x"}, "extra": [1, None]}
        response = await client.post(f"/sim/telnyx/calls/{ccid}/media/inject", json={"frame": frame})
        assert response.status_code == 202, response.text
        await media_agent.wait_for(lambda agent: frame in agent.frames)
        bad = await client.post(f"/sim/telnyx/calls/{ccid}/media/inject", json={"frame": [1]})
        assert bad.status_code == 422


async def test_jitter_swaps_adjacent_frames(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        patched = await client.patch("/sim/telnyx/config", json={"media_jitter": True})
        assert patched.json()["media_jitter"] is True
        await streaming(client, state, media_agent)
        await media_agent.wait_media(6)
        frames = media_agent.media()[:6]
        assert [f["media"]["chunk"] for f in frames] == ["2", "1", "4", "3", "6", "5"]
        assert [f["media"]["timestamp"] for f in frames] == ["20", "0", "60", "40", "100", "80"]
        assert [f["sequence_number"] for f in frames] == ["3", "2", "5", "4", "7", "6"]


async def test_streaming_stop_sends_stop_and_closes(media_agent: MediaAgent) -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        ccid = await streaming(client, state, media_agent)
        await media_agent.wait_media(3)
        await ok(client, ccid, "streaming_stop")
        assert (await client.post(f"/sim/telnyx/calls/{ccid}/say", json={"text": "late"})).status_code == 409
        await settle(state)
        assert media_agent.closed
        assert media_agent.close_code == 1000
        stop = media_agent.frames[-1]
        start = media_agent.frames[1]
        assert stop == {
            "event": "stop",
            "sequence_number": stop["sequence_number"],
            "stop": {"user_id": start["start"]["user_id"], "call_control_id": ccid},
            "stream_id": start["stream_id"],
        }
        sequence = [int(f["sequence_number"]) for f in media_agent.frames[1:]]
        assert sequence == list(range(1, len(sequence) + 1))
        assert [event for event, _ in events(transport, ccid)][-1] == "streaming.stopped"
        assert (await summary(client, ccid))["stream"] is None


async def test_hangup_stops_the_stream_before_call_hangup(media_agent: MediaAgent) -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        ccid = await streaming(client, state, media_agent)
        await ok(client, ccid, "hangup")
        await settle(state)
        assert media_agent.frames[-1]["event"] == "stop"
        assert media_agent.close_code == 1000
        assert [event for event, _ in events(transport, ccid)][-2:] == ["streaming.stopped", "call.hangup"]
        ended = await client.post(f"/sim/telnyx/calls/{ccid}/say", json={"text": "gone"})
        assert (ended.status_code, ended.json()["error"]["code"]) == (409, "call_ended")


async def test_rejected_handshake_is_streaming_failed() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with MediaAgent(reject=True) as agent, telnyx_client(state) as client:
        _, ccid = await answered_call(client, state, stream_url=agent.url)
        await at(state, 1.5)
        [failed] = payloads(transport, "streaming.failed")
        assert failed["failure_reason"] == "connection_failed"
        assert failed["stream_url"] == agent.url
        assert failed["stream_id"]
        assert agent.request_path == "/ws/telnyx?call_id=1"
        assert (await summary(client, ccid))["stream"] is None
        assert error_of(await command(client, ccid, "streaming_stop")) == (422, "10015", None)


async def test_nothing_listening_is_streaming_failed() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        url = f"ws://127.0.0.1:{closed_port()}/ws"
        _, ccid = await answered_call(client, state, stream_url=url)
        await at(state, 1.5)
        assert [p["failure_reason"] for p in payloads(transport, "streaming.failed")] == ["connection_failed"]
        assert [event for event, _ in events(transport, ccid)] == [
            "call.initiated",
            "call.answered",
            "streaming.started",
            "streaming.failed",
        ]


async def test_agent_closing_is_streaming_failed(media_agent: MediaAgent) -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        ccid = await streaming(client, state, media_agent)
        await media_agent.close(1011)
        await media_agent.wait_closed()
        await at(state, state.clock.virtual_ms() / 1000 + 0.1)
        assert [p["failure_reason"] for p in payloads(transport, "streaming.failed")] == ["connection_closed"]
        assert (await summary(client, ccid))["stream"] is None
        assert error_of(await command(client, ccid, "streaming_stop")) == (422, "10015", None)
        assert (await client.post(f"/sim/telnyx/calls/{ccid}/say", json={"text": "hi"})).status_code == 409


async def test_recording_holds_caller_and_agent_tracks(media_agent: MediaAgent) -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        ccid = await streaming(client, state, media_agent, record="record-from-answer")
        said = (await client.post(f"/sim/telnyx/calls/{ccid}/say", json={"text": "record me"})).json()
        reply = ulaw_encode(tone(100))
        await media_agent.send(agent_media(reply))
        await media_agent.send({"event": "mark", "mark": {"name": "replied"}})
        await media_agent.wait_for(lambda agent: bool(agent.of("mark")))
        await media_agent.wait_media(len(media_agent.media()) + said["duration_ms"] // 20 + 2)
        await ok(client, ccid, "hangup")
        await settle(state)
        [saved] = payloads(transport, "call.recording.saved")
        wav = (await client.get(f"/sim/recordings/{saved['recording_id']}.wav")).content
        caller, agent = wav_channels(wav)
        assert decode_marker(caller, 8000) == said["utterance_id"]
        assert ulaw_decode(reply).astype("<i2").tobytes() in agent.tobytes()


async def test_timeline_reports_utterances_and_first_agent_audio(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        ccid = await streaming(client, state, media_agent)
        said = (await client.post(f"/sim/telnyx/calls/{ccid}/say", json={"text": "how late"})).json()
        await media_agent.wait_media(len(media_agent.media()) + said["duration_ms"] // 20 + 3)
        await media_agent.send(agent_media(b"\xff" * 160))
        await media_agent.send({"event": "mark", "mark": {"name": "quiet"}})
        await media_agent.wait_for(lambda agent: len(agent.of("mark")) == 1)
        [entry] = (await client.get(f"/sim/telnyx/calls/{ccid}/timeline")).json()["utterances"]
        assert (entry["utterance_id"], entry["text"]) == (said["utterance_id"], "how late")
        assert entry["end_ms"] - entry["start_ms"] >= said["duration_ms"] - 20
        assert entry["first_agent_audio_ms"] is None
        assert entry["latency_ms"] is None
        await media_agent.send(agent_media(ulaw_encode(tone(40))))
        await media_agent.send({"event": "mark", "mark": {"name": "loud"}})
        await media_agent.wait_for(lambda agent: len(agent.of("mark")) == 2)
        [entry] = (await client.get(f"/sim/telnyx/calls/{ccid}/timeline")).json()["utterances"]
        assert entry["first_agent_audio_ms"] > entry["end_ms"]
        assert entry["latency_ms"] == entry["first_agent_audio_ms"] - entry["end_ms"]


async def test_media_control_errors(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        _, plain = await answered_call(client, state)
        for path, body in (
            ("say", {"text": "hi"}),
            ("dtmf", {"digits": "1"}),
            ("media/inject", {"frame": {}}),
        ):
            response = await client.post(f"/sim/telnyx/calls/{plain}/{path}", json=body)
            assert (response.status_code, response.json()["error"]["code"]) == (409, "not_streaming"), path
            missing = await client.post(f"/sim/telnyx/calls/v3:missing/{path}", json=body)
            assert missing.status_code == 404
        assert (await client.get("/sim/telnyx/calls/v3:missing/agent_audio.wav")).status_code == 404
        silent = await client.get(f"/sim/telnyx/calls/{plain}/agent_audio.wav")
        assert silent.headers["content-type"] == "audio/wav"
        assert wav_channels(silent.content)[0].size == 0
        ccid = await streaming(client, state, media_agent)
        for body in ({}, {"text": "   "}, {"text": "hi", "voice": "x"}):
            assert (await client.post(f"/sim/telnyx/calls/{ccid}/say", json=body)).status_code == 422


async def test_reset_mid_stream_closes_the_socket(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        await streaming(client, state, media_agent)
        response = await client.post("/sim/reset", json={})
        assert response.status_code == 204, response.text
        await media_agent.wait_closed()
        await state.tasks.drain()
        assert state.tasks.pending == 0


async def test_realtime_clock_paces_frames(media_agent: MediaAgent) -> None:
    state = SimState(
        clock=RealtimeClock(),
        latencies=LATENCIES,
        webhook_defaults=WebhookDefaults(voice_url=VOICE_URL, messaging_url=MESSAGING_URL),
        webhook_transport=RecordingTransport(),
    )
    async with telnyx_client(state) as client:
        _, ccid = await answered_call(client, state, stream_url=media_agent.url)
        await media_agent.wait_media(1)
        began = time.monotonic()
        await media_agent.wait_media(11)
        assert time.monotonic() - began >= 0.16
        frames = media_agent.media()[:11]
        assert [int(f["media"]["timestamp"]) for f in frames] == [20 * n for n in range(11)]
        await ok(client, ccid, "hangup")
        await media_agent.wait_closed()
