"""Batch transcript parsing (single, ``separate``, ``combined``) and utterance grouping (specs/08 §8)."""

from __future__ import annotations

from typing import Any

import pytest

from va_elevenlabs.rest_errors import ElevenLabsProtocolError
from va_elevenlabs.stt_batch import BatchEntity, BatchWord, group_utterances, parse_transcript


def entry(  # noqa: PLR0913 - one keyword per vendor field
    text: str,
    start: float,
    end: float,
    *,
    kind: str = "word",
    speaker: str = "speaker_0",
    channel: int | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "text": text,
        "start": start,
        "end": end,
        "type": kind,
        "speaker_id": speaker,
        "logprob": -0.1,
    }
    if channel is not None:
        item["channel_index"] = channel
    return item


def word(
    text: str, start: float, end: float, *, channel: int | None = None, speaker: str | None = None
) -> BatchWord:
    return BatchWord(text, start, end, "word", speaker, channel, None)


def test_single_channel_shape() -> None:
    transcript = parse_transcript(
        {
            "language_code": "en",
            "language_probability": 1.0,
            "text": "Hi there",
            "words": [
                entry("Hi", 0.1, 0.3),
                entry(" ", 0.3, 0.35, kind="spacing"),
                entry("there", 0.35, 0.6, speaker="speaker_1"),
            ],
            "entities": [{"text": "x", "entity_type": "email_address", "start_char": 0, "end_char": 1}],
            "transcription_id": "t1",
            "audio_duration_secs": 2,
        }
    )
    assert transcript.text == "Hi there"
    assert transcript.language_code == "en"
    assert (transcript.transcription_id, transcript.audio_duration_secs) == ("t1", 2.0)
    assert transcript.words[0] == BatchWord("Hi", 0.1, 0.3, "word", "speaker_0", None, -0.1)
    assert [w.type for w in transcript.words] == ["word", "spacing", "word"]
    assert transcript.entities == (BatchEntity("x", "email_address", 0, 1, None),)


def test_separate_channels_merge_by_start_channel_and_order() -> None:
    transcript = parse_transcript(
        {
            "transcripts": [
                {
                    "language_code": "en",
                    "text": "a c",
                    "words": [entry("a", 0.0, 0.2), entry("c", 1.0, 1.2)],
                    "entities": [{"text": "c", "entity_type": "x", "start_char": 2, "end_char": 3}],
                    "channel_index": 0,
                },
                {
                    "language_code": "fr",
                    "text": "b d",
                    "words": [entry("b", 0.5, 0.7, speaker="speaker_1"), entry("d", 1.0, 1.1)],
                    "channel_index": 1,
                },
            ],
            "transcription_id": "t2",
        }
    )
    assert [(w.text, w.channel_index) for w in transcript.words] == [("a", 0), ("b", 1), ("c", 0), ("d", 1)]
    assert transcript.text == "a b c d"
    assert transcript.language_code == "en"
    assert transcript.entities == (BatchEntity("c", "x", 2, 3, 0),)
    assert transcript.audio_duration_secs is None


def test_separate_without_channel_index_uses_the_position() -> None:
    transcript = parse_transcript({"transcripts": [{"words": [entry("a", 0.0, 0.1)]}, {"words": []}]})
    assert transcript.words[0].channel_index == 0
    assert transcript.language_code is None


def test_combined_keeps_the_vendor_order_and_channels() -> None:
    transcript = parse_transcript(
        {
            "language_code": "en",
            "text": "a b",
            "words": [entry("a", 0.0, 0.2, channel=0), entry("b", 0.5, 0.7, channel=1)],
        }
    )
    assert [(w.text, w.channel_index) for w in transcript.words] == [("a", 0), ("b", 1)]
    assert transcript.entities == ()


ENTITY: dict[str, Any] = {"text": "a", "entity_type": "email_address", "start_char": 0, "end_char": 1}


@pytest.mark.parametrize(
    ("document", "reason"),
    [
        ({"words": {}}, "words is not a list"),
        ({"words": [{"start": 0, "end": 1}]}, "words[] entry without text"),
        ({"words": [{"text": "a", "start": "0", "end": 1}]}, "words[].start is not a number"),
        ({"words": [{"text": "a", "start": True, "end": 1}]}, "words[].start is not a number"),
        ({"words": [], "entities": {}}, "entities is not a list"),
        ({"words": [], "entities": ["x"]}, "entities[] entry is not an object"),
        (
            {"words": [], "entities": [{**ENTITY, "start_char": None}]},
            "entities[].start_char is not an integer",
        ),
        (
            {"words": [], "entities": [{**ENTITY, "start_char": "x"}]},
            "entities[].start_char is not an integer",
        ),
        ({"words": [], "entities": [{**ENTITY, "end_char": True}]}, "entities[].end_char is not an integer"),
        ({"words": [], "entities": [{**ENTITY, "end_char": 1.5}]}, "entities[].end_char is not an integer"),
        ({"words": [], "entities": [{**ENTITY, "text": None}]}, "entities[].text is not a string"),
        ({"words": [], "entities": [{**ENTITY, "entity_type": 3}]}, "entities[].entity_type is not a string"),
        ({"words": [], "entities": [{"text": "a"}]}, "entities[].entity_type is not a string"),
        ({"transcripts": {}}, "transcripts is not a list"),
        ({"transcripts": ["x"]}, "transcripts[] entry is not an object"),
    ],
)
def test_malformed_documents(document: dict[str, Any], reason: str) -> None:
    with pytest.raises(ElevenLabsProtocolError) as caught:
        parse_transcript(document)
    assert caught.value.reason == reason


def test_utterances_split_on_role_change() -> None:
    words = [
        word("I", 0.0, 0.2, channel=0),
        word("need", 0.3, 0.5, channel=0),
        word("Sure", 1.0, 1.3, channel=1),
        word("thanks", 1.5, 1.8, channel=0),
    ]
    utterances = group_utterances(words)
    assert [(u.role, u.text, u.start, u.end) for u in utterances] == [
        ("user", "I need", 0.0, 0.5),
        ("assistant", "Sure", 1.0, 1.3),
        ("user", "thanks", 1.5, 1.8),
    ]
    assert utterances[0].words == tuple(words[:2])


def test_utterance_gap_boundary() -> None:
    kept = group_utterances([word("a", 0.0, 1.0, channel=0), word("b", 1.7, 2.0, channel=0)])
    split = group_utterances([word("a", 0.0, 1.0, channel=0), word("b", 1.701, 2.0, channel=0)])
    assert [u.text for u in kept] == ["a b"]
    assert [u.text for u in split] == ["a", "b"]


def test_utterances_sort_words_and_drop_spacing() -> None:
    spacing = BatchWord(" ", 0.2, 0.3, "spacing", None, 0, None)
    event = BatchWord("(laughs)", 0.25, 0.3, "audio_event", None, 0, None)
    utterances = group_utterances(
        [word("b", 0.3, 0.4, channel=0), spacing, event, word("a", 0.0, 0.2, channel=0)]
    )
    assert [u.text for u in utterances] == ["a b"]


def test_mono_speakers_map_to_roles() -> None:
    utterances = group_utterances(
        [
            word("hello", 0.0, 0.3, speaker="speaker_0"),
            word("hi", 0.5, 0.7, speaker="speaker_1"),
            word("um", 0.8, 0.9),
            word("extra", 1.0, 1.2, speaker="speaker_2"),
        ]
    )
    assert [(u.role, u.text) for u in utterances] == [
        ("user", "hello"),
        ("assistant", "hi"),
        ("user", "um"),
        ("assistant", "extra"),
    ]


def test_no_words_no_utterances() -> None:
    assert group_utterances([]) == ()
