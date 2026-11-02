"""Media WebSocket protocol models: inbound parsing and outbound builders (specs/06 §8)."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any, Final

import orjson
import pytest

from va_telnyx import MediaFrame, MediaProtocolError, parse_media_message
from va_telnyx.media import (
    MediaConnected,
    MediaDtmf,
    MediaError,
    MediaErrorCode,
    MediaMark,
    MediaStart,
    MediaStop,
    UnknownMediaEvent,
    clear_message,
    mark_message,
    media_message,
    parse_segment_mark,
    segment_mark,
)

AUDIO: Final = bytes(range(160))
START: Final[dict[str, Any]] = {
    "event": "start",
    "sequence_number": "1",
    "start": {
        "user_id": "u1",
        "call_control_id": "v3:abc",
        "call_session_id": "s1",
        "from": "+14155550100",
        "to": "+14155550111",
        "tags": ["a"],
        "client_state": "eyJ2IjoxfQ==",
        "media_format": {"encoding": "PCMU", "sample_rate": 8000, "channels": 1},
        "custom_parameters": {"new": True},
    },
    "stream_id": "st-1",
}
MEDIA: Final[dict[str, Any]] = {
    "event": "media",
    "sequence_number": "4",
    "media": {
        "track": "inbound",
        "chunk": "3",
        "timestamp": "40",
        "payload": base64.b64encode(AUDIO).decode(),
    },
    "stream_id": "st-1",
}


def _raw(frame: dict[str, Any]) -> str:
    return orjson.dumps(frame).decode()


def test_connected() -> None:
    parsed = parse_media_message(_raw({"event": "connected", "version": "1.0.0", "extra": 1}))
    assert parsed == MediaConnected(event="connected", version="1.0.0")


def test_start_with_unknown_fields() -> None:
    parsed = parse_media_message(_raw(START))
    assert isinstance(parsed, MediaStart)
    assert (parsed.stream_id, parsed.sequence_number) == ("st-1", "1")
    info = parsed.start
    assert (info.call_control_id, info.from_, info.to, info.tags) == (
        "v3:abc",
        "+14155550100",
        "+14155550111",
        ("a",),
    )
    assert (info.media_format.encoding, info.media_format.sample_rate, info.media_format.channels) == (
        "PCMU",
        8000,
        1,
    )


def test_start_with_only_required_fields() -> None:
    frame = {
        "event": "start",
        "sequence_number": "1",
        "stream_id": "st-1",
        "start": {
            "call_control_id": "v3:abc",
            "media_format": {"encoding": "L16", "sample_rate": 16000, "channels": 1},
        },
    }
    parsed = parse_media_message(orjson.dumps(frame))
    assert isinstance(parsed, MediaStart)
    assert (parsed.start.from_, parsed.start.tags, parsed.start.client_state) == (None, (), None)


def test_media_is_a_slots_dataclass_with_ints_and_bytes() -> None:
    parsed = parse_media_message(_raw(MEDIA))
    assert parsed == MediaFrame(chunk=3, timestamp=40, payload=AUDIO, track="inbound")
    assert not hasattr(parsed, "__dict__")


def test_media_accepts_bytes_and_ignores_extra_fields() -> None:
    frame = {**MEDIA, "extra": 1, "media": {**MEDIA["media"], "ssrc": 7}}
    assert parse_media_message(orjson.dumps(frame)) == MediaFrame(3, 40, AUDIO, "inbound")


@pytest.mark.parametrize(
    ("media", "track"), [({"track": "outbound"}, "outbound"), ({"track": 7}, None), ({}, None)]
)
def test_media_track_is_a_string_or_none(media: dict[str, Any], track: str | None) -> None:
    fields = {key: value for key, value in MEDIA["media"].items() if key != "track"}
    frame = {**MEDIA, "media": {**fields, **media}}
    parsed = parse_media_message(orjson.dumps(frame))
    assert isinstance(parsed, MediaFrame)
    assert parsed.track == track


def test_dtmf_stop_error_mark() -> None:
    dtmf = parse_media_message(
        _raw(
            {
                "event": "dtmf",
                "stream_id": "st-1",
                "occurred_at": "2026-11-01T12:00:00.000Z",
                "sequence_number": "9",
                "dtmf": {"digit": "#"},
            }
        )
    )
    assert isinstance(dtmf, MediaDtmf)
    assert (dtmf.dtmf.digit, dtmf.occurred_at) == ("#", datetime(2026, 11, 1, 12, tzinfo=UTC))

    stop = parse_media_message(
        _raw(
            {
                "event": "stop",
                "sequence_number": "10",
                "stop": {"call_control_id": "v3:abc"},
                "stream_id": "st-1",
            }
        )
    )
    assert isinstance(stop, MediaStop)
    assert (stop.stop.call_control_id, stop.stop.user_id) == ("v3:abc", None)

    error = parse_media_message(
        _raw({"event": "error", "payload": {"code": 100003, "title": "malformed_frame", "detail": "bad"}})
    )
    assert isinstance(error, MediaError)
    assert error.payload.code == MediaErrorCode.MALFORMED_FRAME
    assert (error.payload.title, error.payload.detail, error.stream_id) == ("malformed_frame", "bad", None)

    mark = parse_media_message(
        _raw({"event": "mark", "stream_id": "st-1", "sequence_number": "11", "mark": {"name": "seg:1:0"}})
    )
    assert isinstance(mark, MediaMark)
    assert mark.mark.name == "seg:1:0"


def test_unknown_event_is_not_an_error() -> None:
    frame = {"event": "transcription", "text": "hi"}
    assert parse_media_message(_raw(frame)) == UnknownMediaEvent("transcription", frame)


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '"media"',
        '{"no_event": 1}',
        '{"event": 5}',
        _raw({**MEDIA, "media": {"chunk": "3", "timestamp": "40"}}),
        _raw({**MEDIA, "media": {**MEDIA["media"], "chunk": "three"}}),
        _raw({**MEDIA, "media": {**MEDIA["media"], "chunk": None}}),
        _raw({**MEDIA, "media": {**MEDIA["media"], "payload": "not base64!"}}),
        _raw({**MEDIA, "media": "flat"}),
        _raw({**START, "start": {"call_control_id": "v3:abc"}}),
        _raw(
            {
                **START,
                "start": {
                    **START["start"],
                    "media_format": {"encoding": "MP3", "sample_rate": 1, "channels": 1},
                },
            }
        ),
        _raw({"event": "mark", "stream_id": "st-1", "sequence_number": "1"}),
        _raw({"event": "error", "payload": {"code": "x", "title": "t"}}),
    ],
)
def test_malformed_frames_raise_protocol_errors(raw: str) -> None:
    with pytest.raises(MediaProtocolError):
        parse_media_message(raw)


def test_error_codes() -> None:
    assert [(c.name, c.value) for c in MediaErrorCode] == [
        ("UNKNOWN_ERROR", 100002),
        ("MALFORMED_FRAME", 100003),
        ("INVALID_MEDIA", 100004),
        ("RATE_LIMIT_REACHED", 100005),
    ]


# Outbound.


def test_media_message_is_exact_json() -> None:
    assert orjson.loads(media_message(AUDIO)) == {
        "event": "media",
        "media": {"payload": base64.b64encode(AUDIO).decode()},
    }


def test_mark_and_clear_messages_are_exact_json() -> None:
    assert orjson.loads(mark_message("seg:2:1")) == {"event": "mark", "mark": {"name": "seg:2:1"}}
    assert clear_message() == b'{"event":"clear"}'


def test_outbound_builders_reject_empty_values() -> None:
    with pytest.raises(ValueError, match="payload"):
        media_message(b"")
    with pytest.raises(ValueError, match="mark"):
        mark_message("")


def test_segment_marks_round_trip() -> None:
    assert segment_mark(12, 3) == "seg:12:3"
    assert parse_segment_mark(segment_mark(12, 3)) == (12, 3)
    assert parse_segment_mark(segment_mark(0, 0)) == (0, 0)


@pytest.mark.parametrize("name", ["seg:x:1", "seg:1", "seg:1:2:3", "other", "seg:-1:0", " seg:1:2"])
def test_other_mark_names_are_not_segments(name: str) -> None:
    assert parse_segment_mark(name) is None


def test_segment_mark_rejects_negative_indexes() -> None:
    with pytest.raises(ValueError, match="negative"):
        segment_mark(-1, 0)
