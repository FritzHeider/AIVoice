"""Wire format: socket URL, client messages (oracle: specs/09 §3 JSON examples), server frame parsing."""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import orjson
import pytest
from tts_client_helpers import config_for, voice

from va_domain.voices import VoiceSettings
from va_elevenlabs.tts_errors import TtsProtocolError
from va_elevenlabs.tts_models import ChunkAlignment
from va_elevenlabs.tts_wire import (
    AudioFrame,
    ErrorFrame,
    FinalFrame,
    IgnoredFrame,
    close_context_message,
    close_socket_message,
    flush_message,
    init_context,
    keepalive,
    keepalive_init,
    normalize_clause,
    parse_frame,
    socket_url,
    text_message,
)

SPEC = Path(__file__).resolve().parents[3] / "specs" / "09-tts-elevenlabs.md"


def spec_examples() -> list[Any]:
    """The §3 client message examples, in order (init, text, flush, close context)."""
    return [orjson.loads(block) for block in re.findall(r"```json\n(.*?)```", SPEC.read_text(), re.S)]


def test_messages_equal_spec_examples_byte_for_byte_in_key_order() -> None:
    init, text, flush, close = spec_examples()
    schedule = (50, 90, 120, 150)
    assert init_context("t7-0", VoiceSettings(), schedule) == orjson.dumps(init).decode()
    assert text_message("t7-0", "Thanks for calling Acme Dental. ", flush=True) == orjson.dumps(text).decode()
    assert flush_message("t7-0") == orjson.dumps(flush).decode()
    assert close_context_message("t7-0") == orjson.dumps(close).decode()


def test_keepalive_and_close_socket_messages() -> None:
    assert orjson.loads(keepalive_init()) == {"text": " ", "context_id": "ka"}
    assert orjson.loads(keepalive()) == {"text": "", "context_id": "ka"}
    assert orjson.loads(close_socket_message()) == {"close_socket": True}


def test_text_without_flush_omits_the_key() -> None:
    assert orjson.loads(text_message("t1-0", "Hello. ", flush=False)) == {
        "text": "Hello. ",
        "context_id": "t1-0",
    }


def test_init_carries_custom_settings_and_schedule() -> None:
    settings = VoiceSettings(
        stability=0.2, similarity_boost=0.9, style=0.3, use_speaker_boost=False, speed=1.1
    )
    message = orjson.loads(init_context("t2-1-r1", settings, (120, 160)))
    assert message["voice_settings"] == {
        "stability": 0.2,
        "similarity_boost": 0.9,
        "style": 0.3,
        "use_speaker_boost": False,
        "speed": 1.1,
    }
    assert message["generation_config"] == {"chunk_length_schedule": [120, 160]}
    assert message["context_id"] == "t2-1-r1"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Hi", "Hi "), ("Hi  \n", "Hi "), ("  Hi there. ", "  Hi there. "), ("a\tb\t", "a\tb ")],
)
def test_normalize_clause_ends_with_exactly_one_space(raw: str, expected: str) -> None:
    assert normalize_clause(raw) == expected


@pytest.mark.parametrize("raw", ["", " ", "\n\t "])
def test_normalize_clause_rejects_blank_text(raw: str) -> None:
    with pytest.raises(ValueError, match="non-whitespace"):
        normalize_clause(raw)


def _query(url: str) -> list[tuple[str, str]]:
    return parse_qsl(urlsplit(url).query)


def test_socket_url_query_in_spec_table_order() -> None:
    url = socket_url(config_for("ws://h:1"), voice("abc"), "ulaw_8000")
    assert url.startswith("ws://h:1/v1/text-to-speech/abc/multi-stream-input?")
    assert _query(url) == [
        ("model_id", "eleven_flash_v2_5"),
        ("output_format", "ulaw_8000"),
        ("sync_alignment", "true"),
        ("auto_mode", "false"),
        ("inactivity_timeout", "180"),
        ("apply_text_normalization", "auto"),
        ("enable_ssml_parsing", "false"),
    ]


def test_socket_url_language_zero_retention_and_quoting() -> None:
    config = config_for("wss://api.elevenlabs.io/", zero_retention=True, inactivity_timeout=60)
    url = socket_url(config, voice("a b/c", language_code="es", model_id="m2"), "pcm_16000")
    assert urlsplit(url).path == "/v1/text-to-speech/a%20b%2Fc/multi-stream-input"
    assert url.startswith("wss://api.elevenlabs.io/v1/")
    query = _query(url)
    assert ("language_code", "es") in query
    assert query.index(("language_code", "es")) == 6
    assert query[-1] == ("enable_logging", "false")
    assert ("model_id", "m2") in query
    assert ("output_format", "pcm_16000") in query
    assert ("inactivity_timeout", "60") in query


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_parse_audio_frame_with_alignment() -> None:
    raw = orjson.dumps(
        {
            "audio": _b64(b"\x01\x02"),
            "contextId": "t1-0",
            "alignment": {"chars": ["H", "i"], "charStartTimesMs": [0, 60], "charDurationsMs": [60, 60]},
            "normalizedAlignment": None,
        }
    )
    assert parse_frame(raw) == AudioFrame("t1-0", b"\x01\x02", ChunkAlignment(("H", "i"), (0, 60), (60, 60)))


def test_parse_audio_frame_null_alignment_and_null_audio() -> None:
    assert parse_frame('{"audio": null, "contextId": "c", "alignment": null}') == AudioFrame("c", b"", None)
    assert parse_frame(orjson.dumps({"audio": _b64(b"x"), "contextId": "c"})) == AudioFrame("c", b"x", None)


def test_parse_final_error_and_unknown_frames() -> None:
    assert parse_frame('{"isFinal": true, "contextId": "t1-0"}') == FinalFrame("t1-0")
    assert parse_frame('{"message": "m", "error": "quota_exceeded", "code": 1008}') == ErrorFrame(
        "quota_exceeded", "m", 1008
    )
    assert parse_frame('{"error": "x"}') == ErrorFrame("x", "", None)
    assert parse_frame('{"error": "x", "message": "", "code": "1008"}') == ErrorFrame("x", "", None)
    assert parse_frame('{"isFinal": false, "status": "ok"}') == IgnoredFrame(("isFinal", "status"))


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        ("not json", "not JSON"),
        ("[1, 2]", "not a JSON object"),
        ('{"isFinal": true}', "contextId"),
        ('{"audio": "AA==", "contextId": ""}', "contextId"),
        ('{"audio": 5, "contextId": "c"}', "base64 string"),
        ('{"audio": "!!!", "contextId": "c"}', "valid base64"),
        ('{"audio": "AA==", "contextId": "c", "alignment": []}', "object or null"),
        ('{"audio": "AA==", "contextId": "c", "alignment": {"chars": "ab"}}', "chars"),
        (
            '{"audio": "AA==", "contextId": "c", "alignment": '
            '{"chars": ["a"], "charStartTimesMs": [0.5], "charDurationsMs": [1]}}',
            "charStartTimesMs",
        ),
        (
            '{"audio": "AA==", "contextId": "c", "alignment": '
            '{"chars": ["a"], "charStartTimesMs": [0], "charDurationsMs": [true]}}',
            "charDurationsMs",
        ),
        (
            '{"audio": "AA==", "contextId": "c", "alignment": '
            '{"chars": ["a", "b"], "charStartTimesMs": [0], "charDurationsMs": [1]}}',
            "differ in length",
        ),
        ('{"error": 5}', "string error"),
    ],
)
def test_malformed_frames_are_protocol_errors(raw: str, why: str) -> None:
    with pytest.raises(TtsProtocolError, match=why) as caught:
        parse_frame(raw)
    assert caught.value.code == "invalid_server_frame"
