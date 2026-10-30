"""Realtime STT audio analysis without sockets: segmentation, markers, partial cadence, commits, word timings,
vad auto-commit, entities, and chunk decoding."""

from __future__ import annotations

from collections import deque

import numpy as np
import pytest
from numpy.typing import NDArray
from sim_helpers import new_state
from stt_helpers import caller_pcm, encode, silence

from va_sim.audio import synthesize_pcm16
from va_sim.audio.marker import MARKER_MS
from va_sim.elevenlabs.stt_params import AUDIO_FORMATS
from va_sim.elevenlabs.stt_stream import (
    UNINTELLIGIBLE,
    Commit,
    Heard,
    Partial,
    StreamEvent,
    SttStream,
    decode_base64,
    decode_chunk,
)
from va_sim.elevenlabs.stt_text import find_entities, timed_words, words_json
from va_sim.state import SimState

TEXT = "Hello there my friend how are you today"
ULAW = AUDIO_FORMATS["ulaw_8000"]


class Registry:
    """A resolver over a ``SimState``'s utterances plus a script queue, recording its calls."""

    def __init__(self, state: SimState, script: tuple[str, ...] = ()) -> None:
        self.state = state
        self.script = deque(script)
        self.calls: list[int | None] = []

    def __call__(self, marker: int | None) -> Heard:
        self.calls.append(marker)
        if marker is not None:
            utterance = self.state.utterances.get(marker)
            if utterance is None:
                return Heard(UNINTELLIGIBLE, "en", None, has_marker=True)
            return Heard(utterance.text, utterance.language, marker, has_marker=True)
        text = self.script.popleft() if self.script else UNINTELLIGIBLE
        return Heard(text, "en", None, has_marker=False)


def stream(registry: Registry, sample_rate: int = 8_000, vad_silence_ms: int | None = None) -> SttStream:
    return SttStream(sample_rate, registry, language="en", vad_silence_ms=vad_silence_ms)


def feed_in(s: SttStream, pcm: NDArray[np.int16], step: int) -> list[StreamEvent]:
    events: list[StreamEvent] = []
    for start in range(0, pcm.size, step):
        events.extend(s.feed(pcm[start : start + step]))
    return events


def padded(pcm: NDArray[np.int16], rate: int, lead_ms: int = 200, tail_ms: int = 400) -> NDArray[np.int16]:
    fmt = AUDIO_FORMATS["ulaw_8000" if rate == 8_000 else f"pcm_{rate}"]
    return np.concatenate([silence(fmt, lead_ms), pcm, silence(fmt, tail_ms)])


def tone(ms: int, rate: int = 8_000, hz: float = 400.0, dbfs: float = -10.0) -> NDArray[np.int16]:
    t = np.arange(rate * ms // 1000) / rate
    amplitude = 32_767 * 10 ** (dbfs / 20)
    return np.round(amplitude * np.sin(2 * np.pi * hz * t)).astype(np.int16)


def partials(events: list[StreamEvent]) -> list[str]:
    return [event.text for event in events if isinstance(event, Partial)]


@pytest.mark.parametrize("rate", [8_000, 16_000])
def test_utterance_commits_registered_text(rate: int) -> None:
    state = new_state()
    registry = Registry(state)
    s = stream(registry, rate)
    audio = padded(caller_pcm(state, TEXT, rate), rate)
    events = feed_in(s, audio, rate // 10)
    commit = s.commit()
    assert commit.text == TEXT
    assert commit.utterance_ids == (1,)
    assert commit.language == "en"
    assert registry.calls == [1]
    assert not [event for event in events if isinstance(event, Commit)]
    assert s.audio_ms == audio.size * 1000 // rate // 20 * 20  # whole 20 ms frames; the rest is carried


def test_ulaw_round_trip_recovers_text() -> None:
    state = new_state()
    s = stream(Registry(state))
    wire = encode(padded(caller_pcm(state, TEXT, 8_000), 8_000), ULAW)
    s.feed(decode_chunk(wire, ULAW))
    assert s.commit().text == TEXT


def test_partial_cadence_reveals_words_progressively() -> None:
    state = new_state()
    s = stream(Registry(state))
    events = feed_in(s, padded(caller_pcm(state, TEXT, 8_000), 8_000), 800)
    texts = partials(events)
    assert len(texts) == s.voiced_ms // 300
    counts = [len(text.split()) for text in texts]
    assert counts == sorted(counts)
    assert counts[0] <= 2
    assert texts[-1] == TEXT
    assert all(TEXT.startswith(text) for text in texts)


def test_unregistered_marker_is_unintelligible() -> None:
    state = new_state()
    registry = Registry(state)
    s = stream(registry)
    s.feed(padded(synthesize_pcm16(42, "who knows", seed=3), 8_000))
    assert s.commit().text == UNINTELLIGIBLE
    assert registry.calls == [42]


def test_markerless_speech_takes_the_script_then_unintelligible() -> None:
    s = stream(Registry(new_state(), script=("scripted reply",)))
    gap = np.zeros(4_000, dtype=np.int16)
    s.feed(np.concatenate([gap, tone(600), gap]))
    first = s.commit()
    s.feed(np.concatenate([tone(600), gap]))
    second = s.commit()
    assert (first.text, first.utterance_ids) == ("scripted reply", ())
    assert first.words[0].start_ms == 500  # markerless: the body starts with the segment
    assert second.text == UNINTELLIGIBLE


def test_short_click_is_discarded() -> None:
    registry = Registry(new_state(), script=("never used",))
    s = stream(registry)
    events = s.feed(np.concatenate([tone(60), np.zeros(4_000, dtype=np.int16)]))
    assert events == []
    assert s.commit().text == ""
    assert registry.calls == []


def test_two_utterances_join_in_one_window() -> None:
    state = new_state()
    s = stream(Registry(state))
    first = caller_pcm(state, "Good morning", 8_000)
    second = caller_pcm(state, "I need help", 8_000)
    s.feed(np.concatenate([padded(first, 8_000), padded(second, 8_000)]))
    commit = s.commit()
    assert commit.text == "Good morning I need help"
    assert commit.utterance_ids == (1, 2)
    spacing = [entry for entry in words_json(commit.words) if entry["type"] == "spacing"]
    assert len(spacing) == 4


def test_commit_mid_utterance_returns_a_prefix_then_the_rest() -> None:
    state = new_state()
    s = stream(Registry(state))
    audio = padded(caller_pcm(state, TEXT, 8_000), 8_000)
    half = 200 * 8 + audio.size // 3
    s.feed(audio[:half])
    head = s.commit()
    s.feed(audio[half:])
    tail = s.commit()
    assert head.text
    assert tail.text
    assert f"{head.text} {tail.text}" == TEXT
    assert tail.utterance_ids == (1,)


def test_commit_without_voice_is_empty() -> None:
    s = stream(Registry(new_state()))
    s.feed(np.zeros(8_000, dtype=np.int16))
    commit = s.commit()
    assert (commit.text, commit.words, commit.utterance_ids, commit.language) == ("", (), (), "en")


def test_word_timings_follow_the_caller_rate() -> None:
    state = new_state()
    s = stream(Registry(state))
    s.feed(padded(caller_pcm(state, TEXT, 8_000), 8_000, lead_ms=500))
    commit = s.commit()
    body_start = 500 + MARKER_MS
    assert commit.words == timed_words(TEXT, body_start)
    entries = words_json(commit.words)
    assert entries[0] == {"text": "Hello", "start": 0.68, "end": 1.03, "type": "word", "logprob": 0.0}
    assert entries[1] == {"text": " ", "start": 1.03, "end": 1.1, "type": "spacing", "logprob": 0.0}
    starts = [float(str(entry["start"])) for entry in entries]
    assert starts == sorted(starts)
    assert "".join(str(entry["text"]) for entry in entries) == TEXT


def test_feed_split_points_do_not_matter() -> None:
    state = new_state()
    audio = padded(caller_pcm(state, TEXT, 8_000), 8_000)
    whole, pieces = stream(Registry(state)), stream(Registry(state))
    whole_events = whole.feed(audio)
    piece_events = feed_in(pieces, audio, 333)
    assert whole_events == piece_events
    assert whole.commit() == pieces.commit()
    assert (whole.audio_ms, whole.voiced_ms) == (pieces.audio_ms, pieces.voiced_ms)


def test_vad_commits_after_the_silence_threshold() -> None:
    state = new_state()
    s = stream(Registry(state), vad_silence_ms=500)
    pcm = caller_pcm(state, "Book a table", 8_000)
    events = feed_in(s, padded(pcm, 8_000, tail_ms=1_000), 800)
    commits = [event for event in events if isinstance(event, Commit)]
    assert len(commits) == 1
    assert (commits[0].text, commits[0].auto) == ("Book a table", True)
    assert s.commit().text == ""


def test_vad_does_not_commit_before_the_threshold() -> None:
    state = new_state()
    s = stream(Registry(state), vad_silence_ms=1_500)
    events = s.feed(padded(caller_pcm(state, "Book a table", 8_000), 8_000, tail_ms=1_000))
    assert not [event for event in events if isinstance(event, Commit)]


def test_entities_by_category() -> None:
    text = "mail me at jane.doe@mail.test card 4111 1111 1111 1111 thanks"
    both = find_entities(text, ("pii", "pci"))
    assert [(e["entity_type"], e["text"]) for e in both] == [
        ("email_address", "jane.doe@mail.test"),
        ("credit_card_number", "4111 1111 1111 1111"),
    ]
    assert both[0]["start_char"] == text.index("jane")
    assert both[1]["end_char"] == text.index(" thanks")
    assert [e["entity_type"] for e in find_entities(text, ("pii",))] == ["email_address"]
    assert [e["entity_type"] for e in find_entities(text, ("pci",))] == ["credit_card_number"]
    assert len(find_entities(text, ("all",))) == 2
    assert find_entities(text, ("phi",)) == []
    assert find_entities("call 555 1234 567", ("pci",)) == []  # 10 digits: too short


def test_decode_chunk_per_format() -> None:
    pcm = np.array([0, 1_000, -1_000, 32_767], dtype=np.int16)
    pcm16 = AUDIO_FORMATS["pcm_16000"]
    assert decode_chunk(pcm.astype("<i2").tobytes(), pcm16).tolist() == pcm.tolist()
    ulaw = decode_chunk(encode(pcm, ULAW), ULAW)
    assert np.abs(ulaw.astype(np.int32) - pcm).max() < 1_100
    with pytest.raises(ValueError, match="even number of bytes"):
        decode_chunk(b"\x00\x01\x02", pcm16)


def test_decode_base64_is_strict() -> None:
    assert decode_base64("AAE=") == b"\x00\x01"
    with pytest.raises(ValueError, match="not valid base64"):
        decode_base64("not base64!")
