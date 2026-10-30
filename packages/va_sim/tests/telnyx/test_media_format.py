"""Media stream formats: codec/rate selection, unsupported codecs rejected on dial/answer/streaming_start, and
decoding to the 8 kHz call tracks."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
from telnyx_helpers import (
    RecordingTransport,
    answered_call,
    command,
    create_app,
    error_of,
    events,
    ringing_inbound,
    settle,
    summary,
    telnyx_client,
    telnyx_state,
)

from va_sim.audio.codec import ULAW_TO_PCM16, pcm_to_l16be
from va_sim.telnyx.call_models import StreamFields
from va_sim.telnyx.media_format import MediaFormat, agent_format, stream_format

STREAM_URL = "ws://127.0.0.1:9/ws"


def params(**fields: Any) -> dict[str, object]:
    return StreamFields(stream_url=STREAM_URL, **fields).stream_params()


@pytest.mark.parametrize(
    ("fields", "caller", "agent"),
    [
        ({}, ("PCMU", 8000), ("PCMU", 8000)),
        ({"stream_codec": "PCMU"}, ("PCMU", 8000), ("PCMU", 8000)),
        ({"stream_codec": "L16"}, ("L16", 16000), ("L16", 16000)),
        ({"stream_codec": "L16", "stream_bidirectional_sampling_rate": 8000}, ("L16", 8000), ("L16", 8000)),
        (
            {"stream_codec": "L16", "stream_bidirectional_sampling_rate": 16000},
            ("L16", 16000),
            ("L16", 16000),
        ),
        ({"stream_bidirectional_codec": "L16"}, ("PCMU", 8000), ("L16", 8000)),
        (
            {"stream_bidirectional_codec": "L16", "stream_bidirectional_sampling_rate": 16000},
            ("PCMU", 8000),
            ("L16", 16000),
        ),
        ({"stream_codec": "L16", "stream_bidirectional_codec": "PCMU"}, ("L16", 16000), ("PCMU", 8000)),
    ],
)
def test_format_selection(fields: dict[str, Any], caller: tuple[str, int], agent: tuple[str, int]) -> None:
    chosen = params(**fields)
    assert (stream_format(chosen).encoding, stream_format(chosen).sample_rate) == caller
    assert (agent_format(chosen).encoding, agent_format(chosen).sample_rate) == agent


@pytest.mark.parametrize(
    ("fmt", "frame_bytes", "silence_byte"),
    [
        (MediaFormat("PCMU", 8000), 160, 0xFF),
        (MediaFormat("L16", 8000), 320, 0),
        (MediaFormat("L16", 16000), 640, 0),
    ],
)
def test_frame_sizes_and_silence(fmt: MediaFormat, frame_bytes: int, silence_byte: int) -> None:
    assert fmt.frame_bytes == frame_bytes
    assert fmt.silence_frame() == bytes([silence_byte]) * frame_bytes
    assert fmt.decode_pcm8k(fmt.silence_frame()) == bytes(320)


def test_decode_to_8k_tracks() -> None:
    codes = bytes(range(256))
    assert MediaFormat("PCMU", 8000).decode_pcm8k(codes) == ULAW_TO_PCM16.astype("<i2").tobytes()
    samples = np.array([100, 300, -5, -6, 32_767, 32_767, -32_768, -32_767], dtype=np.int16)
    wide = MediaFormat("L16", 16000).decode_pcm8k(pcm_to_l16be(samples))
    assert np.frombuffer(wide, dtype="<i2").tolist() == [200, -6, 32_767, -32_768]
    narrow = MediaFormat("L16", 8000).decode_pcm8k(pcm_to_l16be(samples))
    assert np.frombuffer(narrow, dtype="<i2").tolist() == samples.tolist()


UNSUPPORTED = [
    ({"stream_codec": "PCMA"}, "/stream_codec"),
    ({"stream_codec": "G722"}, "/stream_codec"),
    ({"stream_codec": "OPUS"}, "/stream_codec"),
    ({"stream_codec": "AMR-WB"}, "/stream_codec"),
    ({"stream_bidirectional_codec": "PCMA"}, "/stream_bidirectional_codec"),
    ({"stream_bidirectional_codec": "OPUS"}, "/stream_bidirectional_codec"),
    ({"stream_bidirectional_sampling_rate": 22050}, "/stream_bidirectional_sampling_rate"),
    ({"stream_bidirectional_sampling_rate": 48000}, "/stream_bidirectional_sampling_rate"),
    ({"stream_bidirectional_mode": "mp3"}, "/stream_bidirectional_mode"),
]


@pytest.mark.parametrize(("fields", "pointer"), UNSUPPORTED)
async def test_dial_rejects_unsupported_stream_params(fields: dict[str, Any], pointer: str) -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        app = await create_app(client)
        body = {"connection_id": app["id"], "to": "+14155550101", "from": "+14155552000"}
        response = await client.post("/v2/calls", json={**body, "stream_url": STREAM_URL, **fields})
        assert error_of(response) == (422, "10015", pointer)
        assert list(state.telnyx.all_calls()) == []
        without_stream = await client.post("/v2/calls", json={**body, **fields})
        assert without_stream.status_code == 200, without_stream.text


@pytest.mark.parametrize(("fields", "pointer"), UNSUPPORTED)
async def test_answer_rejects_unsupported_stream_params(fields: dict[str, Any], pointer: str) -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        ccid = await ringing_inbound(client, state)
        response = await command(client, ccid, "answer", stream_url=STREAM_URL, **fields)
        assert error_of(response) == (422, "10015", pointer)
        await settle(state)
        assert [event for event, _ in events(transport, ccid)] == ["call.initiated"]
        assert (await summary(client, ccid))["state"] == "parked"


@pytest.mark.parametrize(("fields", "pointer"), UNSUPPORTED)
async def test_streaming_start_rejects_unsupported_stream_params(
    fields: dict[str, Any], pointer: str
) -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        _, ccid = await answered_call(client, state)
        response = await command(client, ccid, "streaming_start", stream_url=STREAM_URL, **fields)
        assert error_of(response) == (422, "10015", pointer)
        await settle(state)
        detail = await summary(client, ccid)
        assert detail["stream"] is None
        assert [c["name"] for c in detail["commands"]] == ["dial"]
        assert "streaming.started" not in [event for event, _ in events(transport, ccid)]
