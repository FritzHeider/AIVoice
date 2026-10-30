"""Batch STT analysis: audio decoding, voiced segments, TTS tone decoding and matching, caller markers."""

from __future__ import annotations

import io
import wave

import numpy as np
import pytest
from batch_helpers import agent_track, caller_track, place, wav_file
from sim_helpers import new_state

from va_sim.audio import synthesize_pcm16
from va_sim.audio.dtmf import synthesize_dtmf
from va_sim.elevenlabs.stt_batch_audio import (
    BatchAudioError,
    read_batch_audio,
    transcribe,
    transcribe_channel,
    voiced_segments,
)
from va_sim.elevenlabs.stt_batch_tones import decode_tones, map_agent_texts, match_agent_text
from va_sim.elevenlabs.stt_stream import UNINTELLIGIBLE
from va_sim.elevenlabs.tts_audio import tone_class, tone_class_frequencies

LOUD = np.full(8, 3000, dtype=np.int16)


def tone_string(text: str) -> str:
    """``text`` as the decoder spells it: tone classes as letters, whitespace runs as one space."""
    letters = "".join(" " if (k := tone_class(c)) is None else chr(ord("a") + k) for c in text)
    return " ".join(letters.split())


def decoded(samples: np.ndarray, rate: int) -> str:
    [(start, end)] = voiced_segments(samples, rate)
    slots = decode_tones(samples, rate, start, end)
    assert slots is not None
    letters = "".join(" " if s.tone is None else chr(ord("a") + s.tone) for s in slots)
    return " ".join(letters.split())


def square_frames(pattern: list[tuple[bool, int]], rate: int = 8000) -> np.ndarray:
    """Voiced (constant 3000) and silent runs of the given durations in ms."""
    pieces = [np.full(rate * ms // 1000, 3000 if voiced else 0, dtype=np.int16) for voiced, ms in pattern]
    return np.concatenate(pieces)


# Audio decoding.


def test_reads_a_mono_wav() -> None:
    samples = np.arange(1600, dtype=np.int16)
    audio = read_batch_audio(wav_file([samples], 16000), "other", multi_channel=False)
    assert audio.rate == 16000
    assert audio.frames == 1600
    assert audio.duration_s == 0.1
    np.testing.assert_array_equal(audio.channels[0], samples)


def test_stereo_is_kept_with_multi_channel_and_mixed_down_without() -> None:
    left = np.full(800, 30_000, dtype=np.int16)
    right = np.full(800, 10_000, dtype=np.int16)
    data = wav_file([left, right])
    multi = read_batch_audio(data, "other", multi_channel=True)
    assert len(multi.channels) == 2
    np.testing.assert_array_equal(multi.channels[1], right)
    [mono] = read_batch_audio(data, "other", multi_channel=False).channels
    assert set(mono.tolist()) == {32_767}


def test_five_channels_are_accepted_six_are_too_many() -> None:
    tracks = [np.full(800, index * 100, dtype=np.int16) for index in range(6)]
    five = read_batch_audio(wav_file(tracks[:5]), "other", multi_channel=True)
    assert [int(channel[0]) for channel in five.channels] == [0, 100, 200, 300, 400]
    with pytest.raises(BatchAudioError) as caught:
        read_batch_audio(wav_file(tracks), "other", multi_channel=True)
    assert caught.value.status == "too_many_channels"
    assert len(read_batch_audio(wav_file(tracks), "other", multi_channel=False).channels) == 1


def test_raw_pcm_s16le_16() -> None:
    samples = np.arange(-800, 800, dtype=np.int16)
    audio = read_batch_audio(samples.astype("<i2").tobytes(), "pcm_s16le_16", multi_channel=True)
    assert (audio.rate, audio.frames, len(audio.channels)) == (16000, 1600, 1)
    np.testing.assert_array_equal(audio.channels[0], samples)


def eight_bit_wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(1)
        writer.setframerate(8000)
        writer.writeframes(bytes(8000))
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("data", "file_format"),
    [
        (eight_bit_wav(), "other"),
        (b"RIFF garbage that is not a wave file", "other"),
        (wav_file([np.zeros(800, dtype=np.int16)], 4000), "other"),
        (b"\x00" * 3201, "pcm_s16le_16"),
    ],
    ids=["8-bit", "garbage", "low-rate", "odd-pcm"],
)
def test_unsupported_audio_is_invalid(data: bytes, file_format: str) -> None:
    with pytest.raises(BatchAudioError) as caught:
        read_batch_audio(data, file_format, multi_channel=False)
    assert caught.value.status == "invalid_audio"
    assert caught.value.message == "Only 16-bit PCM WAV or pcm_s16le_16 audio is supported by the simulator"


def test_audio_under_100_ms_is_too_short() -> None:
    with pytest.raises(BatchAudioError) as caught:
        read_batch_audio(wav_file([np.zeros(799, dtype=np.int16)]), "other", multi_channel=False)
    assert caught.value.status == "audio_too_short"
    with pytest.raises(BatchAudioError):
        read_batch_audio(b"\x00" * 3198, "pcm_s16le_16", multi_channel=False)


# Voiced segments.


def test_a_280_ms_gap_joins_and_a_320_ms_gap_splits() -> None:
    joined = square_frames([(False, 100), (True, 200), (False, 280), (True, 200), (False, 400)])
    assert voiced_segments(joined, 8000) == [(800, 800 + 8 * 680)]
    split = square_frames([(False, 100), (True, 200), (False, 320), (True, 200), (False, 400)])
    assert voiced_segments(split, 8000) == [(800, 2400), (4960, 6560)]


def test_segments_under_100_ms_of_voice_are_dropped() -> None:
    samples = square_frames([(False, 100), (True, 80), (False, 400), (True, 100), (False, 400)])
    assert voiced_segments(samples, 8000) == [(8 * 580, 8 * 680)]
    assert voiced_segments(np.zeros(100, dtype=np.int16), 8000) == []
    assert voiced_segments(np.zeros(8000, dtype=np.int16), 8000) == []


# Tone decoding.


@pytest.mark.parametrize(("rate", "ulaw"), [(8000, True), (16000, False)])
def test_tones_decode_with_single_and_double_spaces(rate: int, ulaw: bool) -> None:
    text = "Hi  there, how are you?"
    samples = place([(250, agent_track(text, rate, ulaw=ulaw))], 2500, rate)
    assert decoded(samples, rate) == tone_string(text)


def test_tones_resync_after_a_90_ms_gap_between_generations() -> None:
    first, second = agent_track("Hello"), agent_track("World")
    samples = place([(200, first), (200 + 300 + 90, second)], 1500)
    assert decoded(samples, 8000) == tone_string("Hello World")


def test_tone_class_frequencies_follow_the_generator() -> None:
    assert tone_class_frequencies() == tuple(300 + 25 * k for k in range(12))
    assert (tone_class(" "), tone_class("a")) == (None, ord("a") % 12)


def test_caller_speech_and_dtmf_are_not_tones() -> None:
    speech = synthesize_pcm16(5, "hello there", sample_rate=8000, seed=3)
    samples = place([(200, speech)], 2000)
    [(start, end)] = voiced_segments(samples, 8000)
    assert decode_tones(samples, 8000, start, end) is None
    dtmf = np.frombuffer(synthesize_dtmf("5", 200), dtype="<i2").astype(np.int16)
    tones = place([(200, dtmf)], 1000)
    [(start, end)] = voiced_segments(tones, 8000)
    assert decode_tones(tones, 8000, start, end) is None


# Matching.


def agent_words(samples: np.ndarray, texts: list[str]) -> list[tuple[str, float, float]] | None:
    [(start, end)] = voiced_segments(samples, 8000)
    slots = decode_tones(samples, 8000, start, end)
    assert slots is not None
    words = match_agent_text(slots, map_agent_texts(texts), 8000)
    return None if words is None else [(w.text, round(w.start_s, 3), round(w.end_s, 3)) for w in words]


def test_the_first_text_containing_the_decoded_string_wins() -> None:
    samples = place([(500, agent_track("good  day"))], 2000)
    words = agent_words(samples, ["Thanks for calling", "A good\nday to you", "good day"])
    assert words == [("good", 0.5, 0.74), ("day", 0.86, 1.04)]


def test_a_mid_text_start_matches_a_substring() -> None:
    samples = place([(100, agent_track("for calling"))], 1500)
    assert agent_words(samples, ["Thanks for calling us"]) == [("for", 0.1, 0.28), ("calling", 0.34, 0.76)]


def test_back_to_back_generations_match_word_by_word() -> None:
    samples = place([(100, agent_track("Hello")), (490, agent_track("Goodbye"))], 1500)
    words = agent_words(samples, ["Hello", "xq", "Goodbye"])
    assert words == [("Hello", 0.1, 0.4), ("Goodbye", 0.49, 0.91)]
    partly = agent_words(samples, ["Goodbye"])
    assert partly == [(UNINTELLIGIBLE, 0.1, 0.4), ("Goodbye", 0.49, 0.91)]


def test_no_match_is_none_and_transcribes_as_unintelligible() -> None:
    samples = place([(100, agent_track("Hello"))], 1000)
    assert agent_words(samples, ["Different words"]) is None
    [segment] = transcribe_channel(samples, 8000, utterances={}, texts=map_agent_texts([]))
    assert segment.path == "agent"
    assert [(w.text, w.start_s, w.end_s) for w in segment.words] == [(UNINTELLIGIBLE, 0.1, 0.4)]


def test_agent_characters_follow_the_slots() -> None:
    samples = place([(100, agent_track("ok"))], 1000)
    [segment] = transcribe_channel(samples, 8000, utterances={}, texts=map_agent_texts(["ok"]))
    [word] = segment.words
    assert word.characters == (("o", 0.1, 0.16), ("k", 0.16, 0.22))


# Caller markers.


def test_a_caller_marker_resolves_the_registered_text() -> None:
    state = new_state()
    samples = place([(300, caller_track(state, "book a table", language="fr"))], 2500)
    [segment] = transcribe_channel(
        samples, 8000, utterances={1: ("book a table", "fr")}, texts=map_agent_texts([])
    )
    assert (segment.path, segment.language) == ("caller", "fr")
    assert [w.text for w in segment.words] == ["book", "a", "table"]
    [start, _] = voiced_segments(samples, 8000)[0]
    assert segment.words[0].start_s == pytest.approx(start / 8000 + 0.18)
    char, begin, end = segment.words[0].characters[1]
    assert char == "o"
    assert (begin, end) == pytest.approx((0.55, 0.62))


def test_an_unregistered_marker_and_noise_are_unintelligible() -> None:
    state = new_state()
    marked = place([(300, caller_track(state, "hello"))], 1500)
    rng = np.random.default_rng(1)
    noise = place([(200, rng.integers(-4000, 4000, 4000).astype(np.int16))], 1500)
    for samples in (marked, noise):
        [segment] = transcribe_channel(samples, 8000, utterances={}, texts=map_agent_texts([]))
        assert (segment.path, segment.language) == ("caller", None)
        assert [w.text for w in segment.words] == [UNINTELLIGIBLE]


def test_transcribe_handles_every_channel() -> None:
    state = new_state()
    caller = place([(200, caller_track(state, "yes please"))], 2000)
    agent = place([(200, agent_track("Great"))], 2000)
    audio = read_batch_audio(wav_file([caller, agent]), "other", multi_channel=True)
    utterances = {key: (value.text, value.language) for key, value in state.utterances.items()}
    [left, right] = transcribe(audio, utterances, ["Great"])
    assert [w.text for s in left for w in s.words] == ["yes", "please"]
    assert [w.text for s in right for w in s.words] == ["Great"]


def test_a_quiet_onset_less_segment_cannot_decode() -> None:
    samples = np.concatenate([np.zeros(800, dtype=np.int16), LOUD, np.zeros(800, dtype=np.int16)])
    assert decode_tones(samples, 8000, 800, 808) is None
    assert decode_tones(np.zeros(1600, dtype=np.int16), 8000, 0, 1600) is None
