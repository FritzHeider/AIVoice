"""Media WebSocket frames: the shapes the simulator sends and the agent frame parser (specs/06 §8)."""

from __future__ import annotations

import base64

import orjson
import pytest

from va_sim.telnyx.media_format import MediaFormat
from va_sim.telnyx.media_frames import (
    INVALID_MEDIA,
    MALFORMED_FRAME,
    AgentClear,
    AgentMark,
    AgentMedia,
    MediaFault,
    StreamIdentity,
    connected_frame,
    dtmf_frame,
    error_frame,
    mark_frame,
    media_frame,
    parse_agent_frame,
    start_frame,
    stop_frame,
)

PCMU = MediaFormat("PCMU", 8000)
L16 = MediaFormat("L16", 16000)
IDENTITY = StreamIdentity(
    stream_id="stream-1",
    user_id="user-1",
    call_control_id="v3:abc",
    call_session_id="session-1",
    from_="+14155552000",
    to="+14155550101",
    client_state="c3RhdGU=",
)


def test_outbound_frame_shapes() -> None:
    assert connected_frame() == {"event": "connected", "version": "1.0.0"}
    assert start_frame(IDENTITY, L16) == {
        "event": "start",
        "sequence_number": "1",
        "start": {
            "user_id": "user-1",
            "call_control_id": "v3:abc",
            "call_session_id": "session-1",
            "from": "+14155552000",
            "to": "+14155550101",
            "tags": [],
            "client_state": "c3RhdGU=",
            "media_format": {"encoding": "L16", "sample_rate": 16000, "channels": 1},
        },
        "stream_id": "stream-1",
    }
    assert media_frame("stream-1", 7, 3, b"\x01\x02") == {
        "event": "media",
        "sequence_number": "7",
        "media": {"track": "inbound", "chunk": "3", "timestamp": "40", "payload": "AQI="},
        "stream_id": "stream-1",
    }
    assert dtmf_frame("stream-1", 4, "#", "2026-01-01T00:00:01Z") == {
        "event": "dtmf",
        "stream_id": "stream-1",
        "occurred_at": "2026-01-01T00:00:01Z",
        "sequence_number": "4",
        "dtmf": {"digit": "#"},
    }
    assert mark_frame("stream-1", 9, "seg:1:0") == {
        "event": "mark",
        "stream_id": "stream-1",
        "sequence_number": "9",
        "mark": {"name": "seg:1:0"},
    }
    assert error_frame("stream-1", MALFORMED_FRAME, "bad") == {
        "event": "error",
        "payload": {"code": 100003, "title": "malformed_frame", "detail": "bad"},
        "stream_id": "stream-1",
    }
    assert error_frame("stream-1", INVALID_MEDIA, "worse")["payload"]["title"] == "invalid_media"
    assert stop_frame(IDENTITY, 12) == {
        "event": "stop",
        "sequence_number": "12",
        "stop": {"user_id": "user-1", "call_control_id": "v3:abc"},
        "stream_id": "stream-1",
    }


def media(payload: object) -> str:
    return orjson.dumps({"event": "media", "media": {"payload": payload}}).decode()


def test_valid_agent_frames() -> None:
    audio = bytes(range(160))
    assert parse_agent_frame(media(base64.b64encode(audio).decode()), PCMU) == AgentMedia(audio)
    assert parse_agent_frame('{"event": "mark", "mark": {"name": "m1"}}', PCMU) == AgentMark("m1")
    assert parse_agent_frame('{"event": "clear"}', PCMU) == AgentClear()
    thirty_seconds = base64.b64encode(bytes(30_000 * 8)).decode()
    assert parse_agent_frame(media(thirty_seconds), PCMU) == AgentMedia(bytes(240_000))


@pytest.mark.parametrize(
    ("raw", "fmt", "code"),
    [
        (b'{"event": "clear"}', PCMU, MALFORMED_FRAME),
        ("not json", PCMU, MALFORMED_FRAME),
        ("[1, 2]", PCMU, MALFORMED_FRAME),
        ('{"media": {}}', PCMU, MALFORMED_FRAME),
        ('{"event": "start"}', PCMU, MALFORMED_FRAME),
        ('{"event": "mark"}', PCMU, MALFORMED_FRAME),
        ('{"event": "mark", "mark": {"name": ""}}', PCMU, MALFORMED_FRAME),
        ('{"event": "mark", "mark": {"name": 5}}', PCMU, MALFORMED_FRAME),
        ('{"event": "media"}', PCMU, INVALID_MEDIA),
        ('{"event": "media", "media": "AAAA"}', PCMU, INVALID_MEDIA),
        (media(17), PCMU, INVALID_MEDIA),
        (media("not base64!"), PCMU, INVALID_MEDIA),
        (media(""), PCMU, INVALID_MEDIA),
        (media(base64.b64encode(b"\x00\x01\x02").decode()), L16, INVALID_MEDIA),
        (media(base64.b64encode(bytes(30_000 * 8 + 1)).decode()), PCMU, INVALID_MEDIA),
        (media(base64.b64encode(bytes(30_000 * 32 + 2)).decode()), L16, INVALID_MEDIA),
    ],
)
def test_agent_frame_faults(raw: str | bytes, fmt: MediaFormat, code: int) -> None:
    parsed = parse_agent_frame(raw, fmt)
    assert isinstance(parsed, MediaFault)
    assert parsed.code == code
    assert parsed.detail
