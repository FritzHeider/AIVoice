"""Pure parts of the silence policy and DTMF input: NoInputTimer, DtmfBuffer, evaluate_dtmf, merge_turns."""

from __future__ import annotations

import pytest

from va_voice.pipeline import (
    DTMF_SEQUENCE_TIMEOUT_MS,
    BargeInMonitor,
    DtmfBuffer,
    DtmfSequence,
    NoInputTimer,
    UserTurn,
    is_dtmf_digit,
    merge_turns,
)


def deadline(timer: NoInputTimer) -> float | None:
    """The deadline through a call (mypy narrows properties across asserts)."""
    return timer.deadline_ms


def brain_owned(timer: NoInputTimer) -> bool:
    """``brain_owned`` through a call (same reason)."""
    return timer.brain_owned


def test_no_input_timer_arms_once_and_never_extends() -> None:
    timer = NoInputTimer(3000, 2)
    assert deadline(timer) is None
    assert not timer.due(10_000)
    assert timer.arm(100.0)
    assert not timer.arm(2000.0)
    assert deadline(timer) == 3100.0
    assert not timer.due(3099.0)
    assert timer.due(3100.0)
    assert timer.disarm()
    assert not timer.disarm()
    assert deadline(timer) is None


def test_no_input_timer_reprompts_up_to_the_maximum_then_says_goodbye() -> None:
    timer = NoInputTimer(3000, 2)
    actions = []
    for _ in range(3):
        timer.arm(0.0)
        actions.append(timer.expire())
        assert deadline(timer) is None
    assert actions == ["reprompt", "reprompt", "goodbye"]
    assert timer.reprompts == 2
    timer.reset()
    assert (timer.reprompts, timer.expire()) == (0, "reprompt")


def test_brain_owned_wait_uses_its_timeout_and_counts_nothing() -> None:
    timer = NoInputTimer(3000, 1)
    assert timer.arm(100.0, timeout_ms=5000)
    assert brain_owned(timer)
    assert deadline(timer) == 5100.0
    actions = [timer.expire()]
    assert not brain_owned(timer)
    for _ in range(3):
        timer.arm(0.0, timeout_ms=1000)
        actions.append(timer.expire())
    assert actions == ["brain"] * 4
    assert timer.reprompts == 0
    # The configured timeout and the reprompt policy come back once the Brain stops owning the wait.
    assert timer.arm(0.0)
    assert (deadline(timer), brain_owned(timer), timer.expire()) == (3000.0, False, "reprompt")


def test_brain_owned_wait_disarmed_is_not_brain_owned() -> None:
    timer = NoInputTimer(3000, 1)
    timer.arm(0.0, timeout_ms=1000)
    assert timer.disarm()
    assert not brain_owned(timer)
    with pytest.raises(ValueError, match="timeout_ms"):
        timer.arm(0.0, timeout_ms=0)


def test_no_input_timer_without_reprompts_says_goodbye_at_once() -> None:
    assert NoInputTimer(3000, 0).expire() == "goodbye"


@pytest.mark.parametrize(
    ("timeout_ms", "max_reprompts", "message"),
    [(0, 2, "timeout_ms"), (3000, -1, "max_reprompts")],
)
def test_no_input_timer_rejects_invalid_settings(timeout_ms: int, max_reprompts: int, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        NoInputTimer(timeout_ms, max_reprompts)


def test_dtmf_digits() -> None:
    assert all(is_dtmf_digit(d) for d in "0123456789*#ABCD")
    assert not any(is_dtmf_digit(d) for d in ("a", "E", "", "12", " ", "+"))


def test_dtmf_sequence_ends_after_the_timeout() -> None:
    buffer = DtmfBuffer()
    assert (buffer.pending, buffer.deadline_ms) == (False, None)
    assert buffer.press("1", 100.0) is None
    assert buffer.press("2", 300.0) is None
    assert buffer.pending
    assert buffer.deadline_ms == 300.0 + DTMF_SEQUENCE_TIMEOUT_MS
    assert buffer.poll(2299.0) is None
    sequence = buffer.poll(2300.0)
    assert sequence == DtmfSequence("12", 100.0, 300.0)
    assert sequence.text == "[DTMF: 1 2]"
    assert (buffer.pending, buffer.poll(9999.0)) == (False, None)


def test_hash_ends_the_sequence_at_once_and_is_not_part_of_it() -> None:
    buffer = DtmfBuffer()
    assert buffer.press("#", 0.0) is None
    buffer.press("*", 10.0)
    buffer.press("5", 20.0)
    assert buffer.press("#", 30.0) == DtmfSequence("*5", 10.0, 20.0)
    assert not buffer.pending


def test_dtmf_buffer_clear_and_validation() -> None:
    buffer = DtmfBuffer(timeout_ms=500)
    buffer.press("9", 0.0)
    assert buffer.deadline_ms == 500.0
    assert buffer.clear() == 1
    assert buffer.clear() == 0
    with pytest.raises(ValueError, match="not a DTMF digit"):
        buffer.press("x", 0.0)
    with pytest.raises(ValueError, match="timeout_ms"):
        DtmfBuffer(timeout_ms=0)


@pytest.mark.parametrize(
    ("playing", "level", "reason"),
    [(True, 1, "dtmf"), (True, 4, "dtmf"), (True, 0, None), (False, 2, None)],
)
def test_evaluate_dtmf(playing: bool, level: int, reason: str | None) -> None:
    decision = BargeInMonitor().evaluate_dtmf(playing=playing, level=level)
    assert (None if decision is None else decision.reason) == reason


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [("12", "34", "1234"), (None, "34", "34"), ("12", None, "12"), (None, None, None)],
)
def test_merge_turns_keeps_every_digit(first: str | None, second: str | None, expected: str | None) -> None:
    prev = UserTurn(0, "[DTMF: 1 2]" if first else "hello", 0.0, 10.0, dtmf=first)
    new = UserTurn(1, "[DTMF: 3 4]" if second else "there", 20.0, 30.0, dtmf=second)
    assert merge_turns(prev, new).dtmf == expected
