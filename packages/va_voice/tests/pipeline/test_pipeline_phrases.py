"""Cached SpeakStatic playback from a PhraseSource (specs/07 §11 step 5, Decision P05-T10d): no TTS context
for a hit, same outbound clock and alignment timeline, barge-in from the cached alignment, live fallback on a
miss, a failing lookup, or another format."""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import pairwise

from pipeline_fakes import Said, ScriptedBrain, tokens
from pipeline_harness import Harness, harness
from service_helpers import json_log_stream, log_events

from va_voice.audio.formats import PCM16LE_16000, ULAW_8000, AudioFormat
from va_voice.outbound import LEAD_FRAMES
from va_voice.phrase_cache import CachedPhrase, PhraseAlignment
from va_voice.pipeline import PipelineConfig, SpeakStatic, SpeakText
from va_voice.text.pronunciation import PronunciationRule

MS_PER_CHAR = 20
OPENER = (
    "Hello, this is the clinic calling about your appointment next week. I can move it, cancel it, or "
    "confirm it for you, and I can answer any questions about parking or insurance."
)
"""About 170 characters: 3.4 s of cached audio at 20 ms per character."""
FRAME_BYTES = 160


def cached(text: str, *, fmt: AudioFormat = ULAW_8000, with_alignment: bool = True) -> CachedPhrase:
    """``MS_PER_CHAR`` ms of μ-law silence-ish audio per character with exact alignment."""
    bytes_per_ms = 8 if fmt == ULAW_8000 else 32
    audio = bytes([0x55]) * (len(text) * MS_PER_CHAR * bytes_per_ms)
    alignment = PhraseAlignment(
        tuple(text),
        tuple(i * MS_PER_CHAR for i in range(len(text))),
        tuple(MS_PER_CHAR for _ in text),
    )
    return CachedPhrase(
        text, fmt, audio, alignment if with_alignment else None, len(text) * MS_PER_CHAR, cached=True
    )


@dataclass
class DictPhrases:
    entries: dict[str, CachedPhrase] = field(default_factory=dict)
    error: Exception | None = None
    lookups: list[str] = field(default_factory=list)

    async def lookup(self, text: str) -> CachedPhrase | None:
        self.lookups.append(text)
        if self.error is not None:
            raise self.error
        return self.entries.get(text)


def names(h: Harness, *wanted: str) -> list[str]:
    return [name for name, _ in h.transport.calls if name in wanted]


def opener(text: str = OPENER) -> ScriptedBrain:
    return ScriptedBrain(opener=[SpeakStatic(text)])


QUIET = PipelineConfig(no_input_timeout_ms=30_000)


async def test_a_cached_opener_plays_without_a_tts_context() -> None:
    phrase = cached(OPENER)
    phrases = DictPhrases({OPENER: phrase})
    async with harness(brain=opener(), phrases=phrases, config=QUIET) as h:
        await h.until(lambda: len(h.observer.agent_turns) == 1)
        assert phrases.lookups == [OPENER]
        assert h.tts.started == []
        assert h.tts.sent == []
        assert h.tts.flushed == []
        audio = b"".join(frame for _, frame in h.transport.frames)
        assert audio == phrase.audio
        assert len(h.transport.frames) == len(phrase.audio) // FRAME_BYTES
        [turn] = h.observer.agent_turns
        assert (turn.text, turn.text_spoken, turn.interrupted) == (OPENER, OPENER, False)
        # Completed by the mark echo of the single segment.
        assert len(h.transport.marks) == 1
        assert len(h.transport.echoes) == 1


async def test_frames_follow_the_outbound_clock() -> None:
    phrases = DictPhrases({OPENER: cached(OPENER)})
    async with harness(brain=opener(), phrases=phrases, config=QUIET) as h:
        await h.until(lambda: len(h.observer.agent_turns) == 1)
        times = [t for t, _ in h.transport.frames]
        # The first LEAD_FRAMES + 1 frames go out at once, every later one 20 ms after the previous.
        gaps = [round(b - a, 3) for a, b in pairwise(times)]
        assert gaps[:LEAD_FRAMES] == [0.0] * LEAD_FRAMES
        assert set(gaps[LEAD_FRAMES:]) == {20.0}


async def test_barge_in_on_a_cached_opener_truncates_from_the_stored_alignment() -> None:
    phrases = DictPhrases({OPENER: cached(OPENER)})
    async with harness(brain=opener(), phrases=phrases) as h:
        await h.until(lambda: len(h.transport.frames) > 0)
        await h.clock.sleep(0.5)
        await h.caller.say(900)
        await h.until(lambda: len(h.observer.interrupts) == 1)
        await h.until(lambda: len(h.observer.agent_turns) == 1)
        assert names(h, "transport.clear", "tts.cancel") == ["transport.clear"]
        assert h.tts.cancelled == []
        record = h.observer.interrupts[0]
        assert 0 < record.played_chars < record.total_chars == len(OPENER)
        [turn] = h.observer.agent_turns
        assert turn.interrupted
        spoken = turn.text_spoken
        assert spoken
        assert OPENER.startswith(spoken)
        assert h.brain.interrupted == [f"{spoken}—"]


async def test_a_phrase_without_alignment_still_plays() -> None:
    phrase = cached(OPENER, with_alignment=False)
    async with harness(brain=opener(), phrases=DictPhrases({OPENER: phrase}), config=QUIET) as h:
        await h.until(lambda: len(h.observer.agent_turns) == 1)
        assert h.tts.started == []
        assert b"".join(frame for _, frame in h.transport.frames) == phrase.audio
        assert h.observer.agent_turns[0].text == OPENER


async def test_the_lookup_uses_the_pronounced_text() -> None:
    config = PipelineConfig(
        pronunciation=(PronunciationRule("clinic", "klinnik"),), no_input_timeout_ms=30_000
    )
    pronounced = OPENER.replace("clinic", "klinnik")
    phrases = DictPhrases({pronounced: cached(pronounced)})
    async with harness(brain=opener(), phrases=phrases, config=config) as h:
        await h.until(lambda: len(h.observer.agent_turns) == 1)
        assert phrases.lookups == [pronounced]
        assert h.tts.started == []
        assert h.observer.agent_turns[0].text == OPENER


async def test_a_miss_speaks_through_the_live_socket() -> None:
    phrases = DictPhrases()
    async with harness(brain=opener("Hi there."), phrases=phrases, config=QUIET) as h:
        await h.until(lambda: len(h.observer.agent_turns) == 1)
        assert phrases.lookups == ["Hi there."]
        assert len(h.tts.started) == 1
        assert [text for _, text, _ in h.tts.sent] == ["Hi there. "]


async def test_a_failing_lookup_is_logged_and_speaks_live() -> None:
    stream = json_log_stream()
    phrases = DictPhrases(error=ConnectionError("redis down"))
    async with harness(brain=opener("Hi there."), phrases=phrases, config=QUIET) as h:
        await h.until(lambda: len(h.observer.agent_turns) == 1)
        assert [text for _, text, _ in h.tts.sent] == ["Hi there. "]
    [event] = [e for e in log_events(stream) if e["event"] == "pipeline_phrase_lookup_failed"]
    assert event["error_type"] == "ConnectionError"


async def test_a_phrase_in_another_format_speaks_live() -> None:
    phrases = DictPhrases({"Hi there.": cached("Hi there.", fmt=PCM16LE_16000)})
    async with harness(brain=opener("Hi there."), phrases=phrases, config=QUIET) as h:
        await h.until(lambda: len(h.observer.agent_turns) == 1)
        assert len(h.tts.started) == 1


async def test_an_empty_phrase_speaks_live() -> None:
    empty = CachedPhrase("Hi there.", ULAW_8000, b"", None, 0, cached=True)
    async with harness(
        brain=opener("Hi there."), phrases=DictPhrases({"Hi there.": empty}), config=QUIET
    ) as h:
        await h.until(lambda: len(h.observer.agent_turns) == 1)
        assert len(h.tts.started) == 1


async def test_speak_text_and_blank_statics_never_look_up() -> None:
    phrases = DictPhrases()
    brain = ScriptedBrain(opener=[SpeakStatic("   ")])
    async with harness(brain=brain, phrases=phrases, config=QUIET) as h:
        h.brain.opener.append(SpeakText(tokens("Hello from the model.", h.clock)))
        await h.until(lambda: len(h.observer.agent_turns) == 1)
        assert phrases.lookups == []
        assert len(h.tts.started) == 1


async def test_a_cached_reply_reports_immediate_tts_audio() -> None:
    reply = "Your appointment is confirmed."
    phrases = DictPhrases({reply: cached(reply)})
    brain = ScriptedBrain(replies=[[SpeakStatic(reply)]])
    async with harness(
        brain=brain, phrases=phrases, config=QUIET, utterances=[Said("please confirm my visit")]
    ) as h:
        await h.turn()
        await h.until(lambda: len(h.observer.reports) == 1)
        assert h.tts.started == []
        [report] = h.observer.reports
        assert report.latency.tts_ttfa_ms == 0
        assert report.latency.e2e_ms is not None
