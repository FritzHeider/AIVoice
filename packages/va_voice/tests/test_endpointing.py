"""Adaptive endpointer rules on an explicit ms timeline (specs/07-media-pipeline.md §6)."""

from __future__ import annotations

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from va_voice.endpointing import (
    ADAPTIVE_MAX_MS,
    IVR_FLOOR_MS,
    PAUSE_HISTORY,
    TIMEOUT_MAX_MS,
    TIMEOUT_MIN_MS,
    EndOfTurn,
    Endpointer,
    EndpointerSettings,
    ResumptionSpeed,
    TurnDiscarded,
    collects_number,
    completeness_multiplier,
    percentile,
)
from va_voice.vad import VadEvent

NEUTRAL = "i want a table tonight"  # multiplier 1.0
FIXED_LONG = EndpointerSettings(interruption_threshold_ms=5000)


def start(at_ms: float) -> VadEvent:
    return VadEvent("speech_start", at_ms, 0.9, True)


def end(at_ms: float) -> VadEvent:
    return VadEvent("speech_end", at_ms, 0.1, False)


def utter(ep: Endpointer, start_ms: float, end_ms: float, text: str | None = NEUTRAL) -> None:
    """One voiced stretch with a partial arriving mid-speech."""
    ep.on_vad(start(start_ms))
    if text is not None:
        ep.on_partial(text, (start_ms + end_ms) / 2)
    ep.on_vad(end(end_ms))


def deadline(ep: Endpointer) -> float:
    value = ep.deadline_ms()
    assert value is not None
    return value


def end_of_turn(ep: Endpointer, now_ms: float) -> EndOfTurn:
    result = ep.poll(now_ms)
    assert isinstance(result, EndOfTurn)
    return result


def snapshot(ep: Endpointer) -> tuple[bool, bool, float | None, int]:
    """(turn_open, speaking, deadline, pause count) via a function, so mypy does not narrow properties."""
    return ep.turn_open, ep.speaking, ep.deadline_ms(), len(ep.pause_samples)


def record_pauses(ep: Endpointer, pauses: list[float]) -> None:
    """Open a turn and resume speech after each pause (fixed long threshold so no pause ends the turn)."""
    saved = ep.settings
    ep.configure(FIXED_LONG)
    t = 0.0
    utter(ep, t, t + 400)
    t += 400
    for pause in pauses:
        utter(ep, t + pause, t + pause + 400)
        t += pause + 400
    end_of_turn(ep, deadline(ep))
    ep.configure(saved)


# --- base silence by resumption_speed ------------------------------------------------------------


@pytest.mark.parametrize(("speed", "base"), [(1, 300.0), (2, 500.0), (3, 800.0)])
def test_base_silence_by_resumption_speed(speed: ResumptionSpeed, base: float) -> None:
    ep = Endpointer(EndpointerSettings(resumption_speed=speed))
    utter(ep, 1000, 2000)
    assert ep.deadline_ms() == 2000 + base
    assert ep.poll(2000 + base - 0.001) is None
    result = end_of_turn(ep, 2000 + base)
    assert result == EndOfTurn(2000 + base, "silence", 2000, base, NEUTRAL)
    assert result.eot_ms == base
    assert not ep.turn_open


def test_default_settings_are_medium() -> None:
    ep = Endpointer()
    assert ep.settings == EndpointerSettings()
    assert ep.timeout_ms() == 500


# --- completeness multipliers -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("partial", "collecting", "multiplier"),
    [
        ("Yes.", False, 0.7),
        ("thank you", False, 0.7),
        ("Thank you", False, 0.7),
        ("OK", False, 0.7),
        ("Is it?", False, 0.7),
        ("That's great!", False, 0.7),
        ("I went to the.", False, 0.7),  # a terminator wins over the continuation word
        ("and", False, 1.6),
        ("I went to the", False, 1.6),
        ("um", False, 1.6),
        ("my number is", True, 1.6),
        ("my zip is 90210", True, 1.8),
        ("90210.", True, 1.8),  # number rule first
        ("my zip is 90210", False, 1.0),
        ("yes please", False, 1.0),
        ("", False, 1.0),
        (NEUTRAL, False, 1.0),
    ],
)
def test_completeness_multiplier(partial: str, collecting: bool, multiplier: float) -> None:
    assert completeness_multiplier(partial, collecting_number=collecting) == multiplier


@pytest.mark.parametrize(
    ("partial", "collecting", "timeout"),
    [
        ("Yes.", False, 350.0),
        ("I went to the", False, 800.0),
        ("my zip is 90210", True, 900.0),
        (NEUTRAL, False, 500.0),
    ],
)
def test_multiplier_scales_timeout(partial: str, collecting: bool, timeout: float) -> None:
    ep = Endpointer(EndpointerSettings(collecting_number=collecting))
    utter(ep, 0, 1000, partial)
    assert ep.timeout_ms() == pytest.approx(timeout)
    result = end_of_turn(ep, deadline(ep))
    assert result.eot_ms == pytest.approx(timeout)
    assert result.text == partial


def test_later_partial_moves_the_deadline() -> None:
    ep = Endpointer()
    utter(ep, 0, 1000, "Yes.")
    assert deadline(ep) == pytest.approx(1350)
    ep.on_partial("Yes. And", 1100)
    assert deadline(ep) == pytest.approx(1800)
    assert ep.poll(1400) is None
    assert end_of_turn(ep, 1800).text == "Yes. And"


def test_blank_partial_does_not_replace_text() -> None:
    ep = Endpointer()
    utter(ep, 0, 1000, "Yes.")
    ep.on_partial("   ", 1100)
    assert end_of_turn(ep, deadline(ep)).text == "Yes."


# --- clamps and floors --------------------------------------------------------------------------------------


def test_timeout_clamped_to_minimum() -> None:
    ep = Endpointer(EndpointerSettings(resumption_speed=1))
    utter(ep, 0, 1000, "Yes.")
    assert ep.timeout_ms() == TIMEOUT_MIN_MS  # 300 x 0.7 = 210


def test_timeout_clamped_to_maximum() -> None:
    ep = Endpointer(EndpointerSettings(resumption_speed=3))
    record_pauses(ep, [1500, 1500, 1500, 1500])
    assert ep.adaptive_ms() == ADAPTIVE_MAX_MS
    utter(ep, 100_000, 101_000, "and")
    assert ep.timeout_ms() == TIMEOUT_MAX_MS  # 1500 x 1.6 = 2400


@pytest.mark.parametrize(
    ("settings", "partial", "timeout"),
    [
        (EndpointerSettings(resumption_speed=1, ivr_mode=True), NEUTRAL, IVR_FLOOR_MS),
        (EndpointerSettings(resumption_speed=3, ivr_mode=True), "and", 1280.0),
        (EndpointerSettings(interruption_threshold_ms=50, ivr_mode=True), NEUTRAL, IVR_FLOOR_MS),
        (EndpointerSettings(interruption_threshold_ms=1200, ivr_mode=True), "Yes.", 1200.0),
    ],
)
def test_ivr_floor(settings: EndpointerSettings, partial: str, timeout: float) -> None:
    ep = Endpointer(settings)
    utter(ep, 0, 1000, partial)
    assert ep.timeout_ms() == pytest.approx(timeout)


def test_ivr_floor_on_clamped_maximum() -> None:
    ep = Endpointer(EndpointerSettings(resumption_speed=3, ivr_mode=True))
    record_pauses(ep, [1500] * 4)
    utter(ep, 100_000, 101_000, "and")
    assert ep.timeout_ms() == TIMEOUT_MAX_MS


# --- fixed interruption threshold ---------------------------------------------------------------------------


@pytest.mark.parametrize("partial", ["Yes.", "and", "my zip is 90210", NEUTRAL])
def test_fixed_threshold_ignores_multiplier_and_adaptation(partial: str) -> None:
    ep = Endpointer(EndpointerSettings(interruption_threshold_ms=1200, collecting_number=True))
    record_pauses(ep, [1000, 1000, 1000, 1000])
    ep.configure(EndpointerSettings(interruption_threshold_ms=1200, collecting_number=True))
    utter(ep, 100_000, 101_000, partial)
    assert ep.timeout_ms() == 1200
    assert end_of_turn(ep, 102_200).eot_ms == 1200


@pytest.mark.parametrize("threshold", [50, 5000])
def test_threshold_range_bounds_accepted(threshold: int) -> None:
    ep = Endpointer(EndpointerSettings(interruption_threshold_ms=threshold))
    utter(ep, 0, 1000)
    assert ep.timeout_ms() == threshold


@pytest.mark.parametrize(
    "kwargs",
    [
        {"interruption_threshold_ms": 49},
        {"interruption_threshold_ms": 5001},
        {"resumption_speed": 0},
        {"resumption_speed": 4},
    ],
)
def test_invalid_settings_rejected(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="must be"):
        EndpointerSettings(**kwargs)  # type: ignore[arg-type]


# --- adaptation ---------------------------------------------------------------------------------------------


def test_fewer_than_four_pauses_use_base() -> None:
    ep = Endpointer()
    record_pauses(ep, [1000, 1000, 1000])
    assert len(ep.pause_samples) == 3
    assert ep.adaptive_ms() == 500


def test_adaptive_from_p90() -> None:
    ep = Endpointer()
    record_pauses(ep, [200, 300, 400, 600])
    # p90 = 400 + 0.7 x 200 = 540; 1.25 x 540 + 80 = 755
    assert ep.adaptive_ms() == pytest.approx(755)
    utter(ep, 100_000, 101_000)
    assert end_of_turn(ep, deadline(ep)).eot_ms == pytest.approx(755)


def test_adaptive_never_below_base() -> None:
    ep = Endpointer(EndpointerSettings(resumption_speed=3))
    record_pauses(ep, [120, 130, 140, 150])
    assert ep.adaptive_ms() == 800


def test_adaptive_lower_clamp_does_not_go_below_base_or_minimum() -> None:
    ep = Endpointer(EndpointerSettings(resumption_speed=1))
    record_pauses(ep, [120, 120, 120, 120])
    assert ep.adaptive_ms() == 300  # max(300, 230) = 300


@pytest.mark.parametrize(("pause", "kept"), [(119.0, False), (120.0, True), (1500.0, True), (1501.0, False)])
def test_pause_range(pause: float, kept: bool) -> None:
    ep = Endpointer()
    record_pauses(ep, [pause])
    assert ep.pause_samples == ((pause,) if kept else ())


def test_pause_history_keeps_last_twenty() -> None:
    ep = Endpointer()
    pauses = [float(120 + 10 * i) for i in range(25)]
    record_pauses(ep, pauses)
    assert ep.pause_samples == tuple(pauses[-PAUSE_HISTORY:])
    expected = min(max(500, 1.25 * float(np.percentile(pauses[-PAUSE_HISTORY:], 90)) + 80), 1500)
    assert ep.adaptive_ms() == pytest.approx(expected)


def test_pauses_between_turns_are_not_samples() -> None:
    ep = Endpointer()
    utter(ep, 0, 1000)
    end_of_turn(ep, 1500)
    utter(ep, 1700, 2000)
    assert ep.pause_samples == ()


def test_configure_keeps_pauses_and_reset_clears_them() -> None:
    ep = Endpointer()
    record_pauses(ep, [200, 300, 400, 600])
    ep.configure(EndpointerSettings(resumption_speed=1))
    assert ep.adaptive_ms() == pytest.approx(755)
    utter(ep, 100_000, 101_000)
    assert snapshot(ep)[::3] == (True, 4)
    assert deadline(ep) == pytest.approx(101_000 + 755)
    ep.reset()
    assert snapshot(ep) == (False, False, None, 0)
    assert ep.adaptive_ms() == 300


@given(st.lists(st.floats(0, 5000, allow_nan=False), min_size=1, max_size=40), st.floats(0, 100))
def test_percentile_matches_numpy(samples: list[float], q: float) -> None:
    assert percentile(samples, q) == pytest.approx(float(np.percentile(samples, q)))


@pytest.mark.parametrize(("samples", "q"), [([], 90.0), ([1.0], -1.0), ([1.0], 100.5)])
def test_percentile_rejects_bad_input(samples: list[float], q: float) -> None:
    with pytest.raises(ValueError, match=r"percentile|q must"):
        percentile(samples, q)


# --- partial-text rules -------------------------------------------------------------------------------------


def test_no_text_discards_at_cap() -> None:
    ep = Endpointer()
    utter(ep, 1000, 1300, text=None)
    assert ep.text_wait_cap_ms() == TIMEOUT_MAX_MS
    assert deadline(ep) == 1300 + TIMEOUT_MAX_MS
    assert ep.poll(1800) is None  # timeout reached but STT may still be catching up
    assert ep.poll(3299.9) is None
    assert ep.poll(3300) == TurnDiscarded(3300, 1000, 1300)
    assert not ep.turn_open


def test_text_wait_cap_follows_a_longer_timeout() -> None:
    ep = Endpointer(EndpointerSettings(interruption_threshold_ms=3000))
    utter(ep, 0, 1000, text=None)
    assert deadline(ep) == 4000


def test_late_transcript_ends_turn_at_arrival() -> None:
    ep = Endpointer()
    utter(ep, 0, 1000, text=None)
    ep.on_partial(NEUTRAL, 1700)
    assert deadline(ep) == 1500  # already due: the caller polls right away
    result = end_of_turn(ep, 1700)
    assert result.reason == "late_transcript"
    assert result.eot_ms == 700


def test_partial_exactly_at_timeout_is_in_time() -> None:
    ep = Endpointer()
    utter(ep, 0, 1000, text=None)
    ep.on_partial(NEUTRAL, 1500)
    assert end_of_turn(ep, 1500).reason == "silence"


def test_whitespace_partial_is_not_text() -> None:
    ep = Endpointer()
    utter(ep, 0, 1000, text="  \n")
    assert isinstance(ep.poll(3000), TurnDiscarded)


def test_partial_while_idle_is_ignored() -> None:
    ep = Endpointer()
    ep.on_partial("trailing words", 50)
    utter(ep, 100, 500, text=None)
    assert isinstance(ep.poll(2500), TurnDiscarded)


def test_text_spoken_before_a_pause_counts_for_the_turn() -> None:
    ep = Endpointer(FIXED_LONG)
    utter(ep, 0, 1000, "hello there")
    ep.configure(EndpointerSettings())
    utter(ep, 1200, 1800, text=None)
    result = end_of_turn(ep, 1800 + 500)
    assert result.text == "hello there"


# --- resumption, state machine, deadlines -------------------------------------------------------------------


def test_resumed_speech_cancels_pending_end() -> None:
    ep = Endpointer()
    utter(ep, 0, 1000)
    ep.on_vad(start(1300))
    assert ep.poll(1500) is None
    assert ep.deadline_ms() is None
    ep.on_vad(end(2000))
    assert ep.pause_samples == (300.0,)
    result = end_of_turn(ep, 2500)
    assert result.last_voiced_end_ms == 2000
    assert result.eot_ms == 500


def test_next_turn_needs_new_text() -> None:
    ep = Endpointer()
    utter(ep, 0, 1000)
    end_of_turn(ep, 1500)
    utter(ep, 3000, 3400, text=None)
    assert isinstance(ep.poll(5400), TurnDiscarded)


def test_probability_and_duplicate_edges_are_ignored() -> None:
    ep = Endpointer()
    ep.on_vad(end(10))  # end while silent
    ep.on_vad(VadEvent("probability", 20, 0.9, True))
    assert not ep.turn_open
    ep.on_vad(start(100))
    ep.on_vad(start(150))  # repeated start
    assert ep.speaking
    ep.on_partial(NEUTRAL, 200)
    ep.on_vad(end(600))
    ep.on_vad(end(700))  # repeated end
    assert end_of_turn(ep, 1100).last_voiced_end_ms == 600


def test_deadline_per_state() -> None:
    ep = Endpointer()
    assert ep.deadline_ms() is None
    assert ep.poll(10_000) is None
    ep.on_vad(start(0))
    assert snapshot(ep) == (True, True, None, 0)
    ep.on_vad(end(800))
    assert snapshot(ep) == (True, False, 800 + TIMEOUT_MAX_MS, 0)
    ep.on_partial(NEUTRAL, 900)
    assert snapshot(ep) == (True, False, 1300, 0)


@pytest.mark.parametrize(
    ("variables", "expected"),
    [
        ([("amount", "integer")], True),
        ([("phone_number", "string")], True),
        ([("ZIP", "string")], True),
        ([("promo_code", "string")], True),
        ([("Postcode", "string")], True),  # name contains "code"
        ([("zipper", "string")], True),  # substring rule, as specified
        ([("name", "string"), ("age", "integer")], True),
        ([("name", "string")], False),
        ([], False),
    ],
)
def test_collects_number(variables: list[tuple[str, str]], expected: bool) -> None:
    assert collects_number(variables) is expected
