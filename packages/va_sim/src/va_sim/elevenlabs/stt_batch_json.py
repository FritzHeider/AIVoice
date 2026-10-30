"""Batch STT response bodies (specs/30 §3.3; Decision P03-T05b rules 7-8).

One transcript per channel: ``words`` alternate word and spacing entries (seconds, 3 decimals, relative to
the file start), ``text`` joins the words with single spaces, and ``language_code`` is the first caller
utterance language heard (else the request ``language_code``, else ``en``). Without ``use_multi_channel`` the
body is the single-channel shape; with it, ``separate`` lists ``transcripts`` and ``combined`` merges every
channel's entries by start time into the single-channel shape.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from va_sim.elevenlabs.stt_batch_audio import BatchSegment
from va_sim.elevenlabs.stt_batch_params import BatchForm
from va_sim.elevenlabs.stt_batch_tones import BatchWord
from va_sim.elevenlabs.stt_text import find_entities

__all__ = ["batch_response"]

_DEFAULT_LANGUAGE: Final = "en"
_DECIMALS: Final = 3

type Entry = dict[str, object]


@dataclass(frozen=True, slots=True)
class _Transcript:
    channel: int
    entries: list[Entry]
    starts: list[float]
    text: str
    caller_language: str | None


def _seconds(value: float) -> float:
    return round(value, _DECIMALS)


def _speaker(segment: BatchSegment, channel: int, form: BatchForm) -> str:
    if form.use_multi_channel:
        return f"speaker_{channel}"
    if form.diarize and segment.path == "agent":
        return "speaker_1"
    return "speaker_0"


def _word_entry(word: BatchWord, speaker: str, form: BatchForm) -> Entry:
    entry: Entry = {
        "text": word.text,
        "start": _seconds(word.start_s),
        "end": _seconds(word.end_s),
        "type": "word",
        "speaker_id": speaker,
        "logprob": 0.0,
    }
    if form.timestamps_granularity == "character":
        entry["characters"] = [
            {"text": char, "start": _seconds(start), "end": _seconds(end)}
            for char, start, end in word.characters
        ]
    return entry


def _spacing(previous: Entry, following: Entry) -> Entry:
    return {
        "text": " ",
        "start": previous["end"],
        "end": following["start"],
        "type": "spacing",
        "speaker_id": previous["speaker_id"],
        "logprob": 0.0,
    }


def _transcript(segments: Sequence[BatchSegment], channel: int, form: BatchForm) -> _Transcript:
    entries: list[Entry] = []
    starts: list[float] = []
    texts: list[str] = []
    previous: BatchWord | None = None
    for segment in segments:
        speaker = _speaker(segment, channel, form)
        for word in segment.words:
            entry = _word_entry(word, speaker, form)
            if previous is not None:
                entries.append(_spacing(entries[-1], entry))
                starts.append(_seconds(previous.end_s))
            entries.append(entry)
            starts.append(_seconds(word.start_s))
            texts.append(word.text)
            previous = word
    if form.use_multi_channel:
        for entry in entries:
            entry["channel_index"] = channel
    language = next((segment.language for segment in segments if segment.language is not None), None)
    return _Transcript(channel, entries, starts, " ".join(texts), language)


def _language(language: str | None, form: BatchForm) -> str:
    return language or form.language_code or _DEFAULT_LANGUAGE


def _body(transcript: _Transcript, form: BatchForm) -> dict[str, object]:
    body: dict[str, object] = {
        "language_code": _language(transcript.caller_language, form),
        "language_probability": 1.0,
        "text": transcript.text,
        "words": [] if form.timestamps_granularity == "none" else transcript.entries,
    }
    if form.entity_detection:
        body["entities"] = find_entities(transcript.text, form.entity_detection)
    return body


def _combined(transcripts: Sequence[_Transcript], form: BatchForm) -> dict[str, object]:
    keyed = [
        (start, transcript.channel, order, entry)
        for transcript in transcripts
        for order, (start, entry) in enumerate(zip(transcript.starts, transcript.entries, strict=True))
    ]
    ordered = [entry for *_, entry in sorted(keyed, key=lambda item: item[:3])]
    language = next((t.caller_language for t in transcripts if t.caller_language is not None), None)
    text = " ".join(str(entry["text"]) for entry in ordered if entry["type"] == "word")
    return {
        "language_code": _language(language, form),
        "language_probability": 1.0,
        "text": text,
        "words": ordered,
    }


def batch_response(
    channels: Sequence[Sequence[BatchSegment]], form: BatchForm, *, transcription_id: str, duration_s: float
) -> dict[str, object]:
    """The response body for the segments of every channel."""
    transcripts = [_transcript(segments, index, form) for index, segments in enumerate(channels)]
    trailer: dict[str, object] = {
        "transcription_id": transcription_id,
        "audio_duration_secs": _seconds(duration_s),
    }
    if not form.use_multi_channel:
        return {**_body(transcripts[0], form), **trailer}
    if form.multichannel_output_style == "combined":
        return {**_combined(transcripts, form), **trailer}
    return {
        "transcripts": [{**_body(t, form), "channel_index": t.channel} for t in transcripts],
        **trailer,
    }
