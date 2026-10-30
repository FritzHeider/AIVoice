"""TTS context: chunk_length_schedule triggers, flush, whitespace buffers, schedule validation."""

from __future__ import annotations

import pytest

from va_sim.elevenlabs.store import ElevenLabsStore
from va_sim.elevenlabs.tts_context import DEFAULT_SCHEDULE, TtsContext, parse_schedule


def context(schedule: tuple[int, ...] = DEFAULT_SCHEDULE) -> TtsContext:
    record = ElevenLabsStore().new_request(
        transport="ws", voice_id="v", model_id="m", output_format="ulaw_8000", context_id="c", started_ms=0
    )
    return TtsContext("c", schedule, record)


def test_schedule_threshold_triggers_without_flush() -> None:
    ctx = context((50,))
    ctx.add_text("x" * 49)
    assert ctx.take(flush=False) is None
    ctx.add_text("x")
    assert ctx.take(flush=False) == "x" * 50
    assert ctx.buffer == ""
    assert ctx.record.generated_text == "x" * 50
    assert ctx.record.characters == 50


def test_schedule_progresses_per_generation_and_sticks_at_the_last() -> None:
    ctx = context((50, 60))
    assert ctx.threshold() == 50
    ctx.add_text("a" * 50)
    assert ctx.take(flush=False) is not None
    assert ctx.threshold() == 60
    ctx.add_text("b" * 59)
    assert ctx.take(flush=False) is None
    ctx.add_text("b")
    assert ctx.take(flush=False) == "b" * 60
    assert ctx.threshold() == 60


def test_threshold_counts_the_stripped_buffer() -> None:
    ctx = context((50,))
    ctx.add_text(" " + "x" * 49 + "   ")
    assert ctx.take(flush=False) is None


def test_flush_generates_the_whole_buffer_verbatim() -> None:
    ctx = context()
    ctx.add_text("Hello ")
    ctx.add_text("there. ")
    assert ctx.take(flush=True) == "Hello there. "
    assert ctx.record.text == "Hello there. "


def test_whitespace_only_buffer_never_generates() -> None:
    ctx = context()
    ctx.add_text(" ")
    assert ctx.take(flush=False) is None
    assert ctx.buffer == " "
    assert ctx.take(flush=True) is None
    assert ctx.buffer == ""
    assert ctx.generations == 0


def test_discard_reports_dropped_text() -> None:
    ctx = context()
    ctx.add_text("  ")
    assert ctx.discard() is False
    ctx.add_text("words")
    assert ctx.discard() is True
    assert ctx.buffer == ""


def test_parse_schedule_defaults_and_accepts_valid_lists() -> None:
    assert parse_schedule(None) == DEFAULT_SCHEDULE
    assert parse_schedule({}) == DEFAULT_SCHEDULE
    assert parse_schedule({"chunk_length_schedule": [50, 90, 120, 150]}) == (50, 90, 120, 150)


@pytest.mark.parametrize(
    "config",
    [
        "fast",
        {"chunk_length_schedule": []},
        {"chunk_length_schedule": [50, 60, 70, 80, 90]},
        {"chunk_length_schedule": [49]},
        {"chunk_length_schedule": [501]},
        {"chunk_length_schedule": [True]},
        {"chunk_length_schedule": ["120"]},
        {"chunk_length_schedule": 120},
    ],
)
def test_parse_schedule_rejects_invalid(config: object) -> None:
    with pytest.raises(ValueError, match=r"generation_config|chunk_length_schedule"):
        parse_schedule(config)
