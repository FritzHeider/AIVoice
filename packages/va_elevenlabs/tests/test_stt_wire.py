"""Realtime STT wire format: URL for every option combination, the audio chunk message vs the specs/08 §4
example, silence frames, and parsing of every server message (specs/08 §2-§4)."""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any

import orjson
import pytest
from stt_client_helpers import committed_words, entities, server_error

from va_elevenlabs import SttConfig, SttConnection, SttEntity, SttProtocolError, SttWord
from va_elevenlabs.stt_errors import ACTIONS
from va_elevenlabs.stt_wire import (
    Committed,
    CommittedWords,
    Entities,
    Ignored,
    Partial,
    ServerError,
    SessionStarted,
    audio_chunk,
    bytes_per_ms,
    parse_message,
    realtime_url,
    silence_frame,
)

SPEC = Path(__file__).parents[3] / "specs" / "08-stt-elevenlabs.md"
BASE = "wss://api.elevenlabs.io"
PREFIX = f"{BASE}/v1/speech-to-text/realtime?"
HEAD = "model_id=scribe_v2_realtime&audio_format=ulaw_8000&commit_strategy=manual&include_timestamps=true"
WORDS_PREFIX = '{"message_type": "committed_transcript_with_timestamps", "text": "", "words": '
TAIL = "keepalive_interval_ms=5000&no_verbatim=false"


def conn(**fields: Any) -> SttConnection:
    return SttConnection(ws_base=fields.pop("ws_base", BASE), api_key="k", **fields)


@pytest.mark.parametrize(
    ("connection", "cfg", "minimal", "query"),
    [
        (
            conn(),
            SttConfig(),
            False,
            f"{HEAD}&include_language_detection=true&filter_background_audio=false&{TAIL}",
        ),
        (
            conn(ws_base=f"{BASE}/"),
            SttConfig(language_code="es", include_language_detection=False, keyterms=["Blandy", "Acme Corp"]),
            False,
            f"{HEAD}&include_language_detection=false&language_code=es&keyterms=Blandy&keyterms=Acme+Corp"
            f"&filter_background_audio=false&{TAIL}",
        ),
        (
            conn(keyterms_encoding="json", zero_retention=True),
            SttConfig(keyterms=["a", "b c"], filter_background_audio=True, entity_detection=["pii", "pci"]),
            False,
            f"{HEAD}&include_language_detection=true&keyterms=%5B%22a%22%2C%22b+c%22%5D"
            "&filter_background_audio=true&entity_detection=pii&entity_detection=pci"
            "&keepalive_interval_ms=5000&enable_logging=false&no_verbatim=false",
        ),
        (
            conn(),
            SttConfig(keyterms=["Acme"], entity_detection=["pii"]),
            True,
            f"{HEAD}&include_language_detection=true&filter_background_audio=false&{TAIL}",
        ),
        (
            conn(model_id="scribe_v3", keepalive_interval_ms=2000),
            SttConfig(audio_format="pcm_16000", sample_rate=16000),
            False,
            "model_id=scribe_v3&audio_format=pcm_16000&commit_strategy=manual&include_timestamps=true"
            "&include_language_detection=true&filter_background_audio=false&keepalive_interval_ms=2000"
            "&no_verbatim=false",
        ),
    ],
    ids=["defaults", "language-keyterms", "json-zero-retention-entities", "minimal", "pcm16-model"],
)
def test_realtime_url(connection: SttConnection, cfg: SttConfig, minimal: bool, query: str) -> None:
    assert realtime_url(connection, cfg, minimal=minimal) == PREFIX + query


def spec_chunk_example() -> dict[str, Any]:
    text = SPEC.read_text(encoding="utf-8")
    match = re.search(r'```json\n(\{"message_type": "input_audio_chunk".*?\})\n```', text)
    assert match is not None
    example: dict[str, Any] = orjson.loads(match.group(1))
    return example


def test_chunk_matches_the_spec_example() -> None:
    example = spec_chunk_example()
    message = orjson.loads(
        audio_chunk(b"\x01\x02", commit=False, sample_rate=8000, previous_text=example["previous_text"])
    )
    assert list(message) == list(example)
    assert {key: message[key] for key in message if key != "audio_base_64"} == {
        key: example[key] for key in example if key != "audio_base_64"
    }
    assert base64.b64decode(message["audio_base_64"]) == b"\x01\x02"


def test_chunk_omits_previous_text_when_none() -> None:
    message = orjson.loads(audio_chunk(b"\xff", commit=True, sample_rate=16000))
    assert message == {
        "message_type": "input_audio_chunk",
        "audio_base_64": base64.b64encode(b"\xff").decode(),
        "commit": True,
        "sample_rate": 16000,
    }


def test_silence_frames_and_rates() -> None:
    assert silence_frame("ulaw_8000") == b"\xff" * 160
    assert silence_frame("pcm_16000") == bytes(640)
    assert (bytes_per_ms("ulaw_8000"), bytes_per_ms("pcm_16000")) == (8, 32)


def test_parse_data_messages() -> None:
    started = {
        "message_type": "session_started",
        "session_id": "abc",
        "config": {
            "audio_format": "ulaw_8000",
            "sample_rate": 8000,
            "commit_strategy": "manual",
            "keyterms": ["x"],
        },
    }
    assert parse_message(orjson.dumps(started)) == SessionStarted("abc", "ulaw_8000", 8000, "manual", ("x",))
    assert parse_message('{"message_type": "partial_transcript", "text": "hi"}') == Partial("hi")
    assert parse_message('{"message_type": "committed_transcript", "text": "hi"}') == Committed("hi")
    words = parse_message(orjson.dumps(committed_words("hello there", "es")))
    assert words == CommittedWords(
        "hello there",
        "es",
        (
            SttWord(text="hello", start_s=0.0, end_s=0.08, logprob=-0.1),
            SttWord(text="there", start_s=0.1, end_s=0.18, logprob=-0.1),
        ),
    )
    entity = {"text": "a@b.test", "entity_type": "email_address", "start_char": 0, "end_char": 8, "extra": 1}
    assert parse_message(orjson.dumps(entities("a@b.test", [entity]))) == Entities(
        "a@b.test", (SttEntity(text="a@b.test", entity_type="email_address", start_char=0, end_char=8),)
    )


def test_words_without_logprob_and_null_language() -> None:
    frame = {
        "message_type": "committed_transcript_with_timestamps",
        "text": "",
        "language_code": None,
        "words": [{"text": "ok", "start": 1, "end": 2}],
    }
    assert parse_message(orjson.dumps(frame)) == CommittedWords(
        "", None, (SttWord(text="ok", start_s=1.0, end_s=2.0),)
    )


@pytest.mark.parametrize("message_type", sorted(ACTIONS))
def test_every_error_row_parses_as_server_error(message_type: str) -> None:
    assert parse_message(orjson.dumps(server_error(message_type, "why"))) == ServerError(message_type, "why")


@pytest.mark.parametrize("message_type", ["edited_transcript", "insufficient_audio_activity", "future_type"])
def test_ignored_types(message_type: str) -> None:
    assert parse_message(orjson.dumps({"message_type": message_type})) == Ignored(message_type)


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[1]",
        "{}",
        '{"message_type": ""}',
        '{"message_type": "partial_transcript"}',
        '{"message_type": "committed_transcript", "text": 3}',
        '{"message_type": "session_started", "session_id": "a"}',
        '{"message_type": "session_started", "session_id": "a", "config": {"keyterms": [1]}}',
        '{"message_type": "committed_transcript_with_timestamps", "text": "", "words": {}}',
        WORDS_PREFIX + '[{"text": "a", "start": "x", "end": 1}]}',
        WORDS_PREFIX + '[{"text": "a", "start": true, "end": 1}]}',
        '{"message_type": "committed_transcript_with_timestamps", "text": "", "language_code": 1}',
        '{"message_type": "committed_transcript_entities", "text": "", "entities": [1]}',
        '{"message_type": "committed_transcript_entities", "text": "", "entities": [{"text": "a"}]}',
        '{"message_type": "warning", "error": 5}',
    ],
)
def test_malformed_frames_are_protocol_errors(raw: str) -> None:
    with pytest.raises(SttProtocolError) as caught:
        parse_message(raw)
    assert caught.value.code == "invalid_server_frame"
