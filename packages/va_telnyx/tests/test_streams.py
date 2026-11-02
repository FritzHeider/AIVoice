"""Shared media stream and recording parameters (``va_telnyx.streams``)."""

from __future__ import annotations

import uuid
from urllib.parse import parse_qs, urlsplit

from va_telnyx.streams import RECORDING_FIELDS, media_stream_url, stream_params

CALL_ID = uuid.UUID("01890000-0000-7000-8000-000000000001")


def test_the_stream_url_carries_the_call_id_and_an_encoded_token() -> None:
    url = media_stream_url("wss", "media.test", CALL_ID, "a+b/c=")
    parts = urlsplit(url)
    assert (parts.scheme, parts.netloc, parts.path) == ("wss", "media.test", "/ws/telnyx")
    assert parse_qs(parts.query) == {"call_id": [str(CALL_ID)], "token": ["a+b/c="]}
    assert "a%2Bb%2Fc%3D" in url


def test_narrowband_streams_use_pcmu_at_8_khz() -> None:
    params = stream_params("ws://m.test/ws/telnyx", hd_audio=False)
    assert params.body() == {
        "stream_url": "ws://m.test/ws/telnyx",
        "stream_track": "inbound_track",
        "stream_codec": "PCMU",
        "stream_bidirectional_mode": "rtp",
        "stream_bidirectional_codec": "PCMU",
        "stream_bidirectional_sampling_rate": 8000,
    }


def test_hd_audio_streams_use_l16_at_16_khz() -> None:
    params = stream_params("ws://m.test/ws/telnyx", hd_audio=True)
    assert (
        params.stream_codec,
        params.stream_bidirectional_codec,
        params.stream_bidirectional_sampling_rate,
    ) == ("L16", "L16", 16000)


def test_recording_fields_record_dual_channel_wav_from_the_answer() -> None:
    assert dict(RECORDING_FIELDS) == {
        "record": "record-from-answer",
        "record_channels": "dual",
        "record_format": "wav",
    }
