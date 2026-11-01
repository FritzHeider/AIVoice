"""Circuit breaker transitions on a frozen clock (specs/10 §2.2; Decision P03-T13a rule 7)."""

from __future__ import annotations

from service_helpers import json_log_stream, log_events

from va_common.time import FrozenClock
from va_llm.breaker import BreakerBoard, BreakerState, CircuitBreaker


def _state(breaker: CircuitBreaker) -> BreakerState:
    """The state, read through a call (mypy keeps a narrowed property across calls)."""
    return breaker.state


def _fail(breaker: CircuitBreaker, times: int) -> None:
    for _ in range(times):
        assert breaker.allow()
        breaker.record(success=False)


def _opened() -> tuple[FrozenClock, CircuitBreaker, list[tuple[BreakerState, BreakerState]]]:
    clock = FrozenClock()
    moves: list[tuple[BreakerState, BreakerState]] = []
    breaker = CircuitBreaker(clock, on_transition=lambda a, b: moves.append((a, b)))
    _fail(breaker, 5)
    return clock, breaker, moves


def test_four_failures_stay_closed_and_the_fifth_within_30_s_opens() -> None:
    clock = FrozenClock()
    breaker = CircuitBreaker(clock)
    for _ in range(4):
        _fail(breaker, 1)
        clock.advance(7)
    assert _state(breaker) == "closed"
    _fail(breaker, 1)
    assert _state(breaker) == "open"
    assert not breaker.allow()


def test_failures_older_than_the_window_drop_out() -> None:
    clock = FrozenClock()
    breaker = CircuitBreaker(clock)
    _fail(breaker, 4)
    clock.advance(30.5)
    _fail(breaker, 4)
    assert _state(breaker) == "closed"
    _fail(breaker, 1)
    assert _state(breaker) == "open"


def test_successes_do_not_count_as_failures() -> None:
    clock = FrozenClock()
    breaker = CircuitBreaker(clock)
    for _ in range(10):
        assert breaker.allow()
        breaker.record(success=True)
    _fail(breaker, 4)
    assert _state(breaker) == "closed"


def test_open_turns_half_open_after_20_s_and_admits_exactly_one_probe() -> None:
    clock, breaker, moves = _opened()
    clock.advance(19.9)
    assert not breaker.allow()
    clock.advance(0.1)
    assert breaker.allow()
    assert _state(breaker) == "half_open"
    assert not breaker.allow()
    assert moves == [("closed", "open"), ("open", "half_open")]


def test_probe_success_closes_and_clears_the_window() -> None:
    clock, breaker, moves = _opened()
    clock.advance(20)
    assert breaker.allow()
    breaker.record(success=True)
    assert _state(breaker) == "closed"
    assert moves[-1] == ("half_open", "closed")
    _fail(breaker, 4)
    assert _state(breaker) == "closed"


def test_probe_failure_reopens_for_another_20_s() -> None:
    clock, breaker, moves = _opened()
    clock.advance(20)
    assert breaker.allow()
    breaker.record(success=False)
    assert _state(breaker) == "open"
    assert moves[-1] == ("half_open", "open")
    clock.advance(19)
    assert not breaker.allow()
    clock.advance(1)
    assert breaker.allow()


def test_an_abandoned_probe_frees_the_half_open_slot() -> None:
    clock, breaker, _ = _opened()
    clock.advance(20)
    assert breaker.allow()
    assert not breaker.allow()
    breaker.abandon()
    assert breaker.allow()
    assert _state(breaker) == "half_open"


def test_late_verdicts_while_open_change_nothing() -> None:
    clock, breaker, moves = _opened()
    breaker.record(success=True)
    breaker.record(success=False)
    assert _state(breaker) == "open"
    assert len(moves) == 1
    clock.advance(20)
    assert breaker.allow()


def test_board_keys_breakers_by_provider_and_model_and_reports_states() -> None:
    clock = FrozenClock()
    states: list[tuple[str, str, BreakerState]] = []
    board = BreakerBoard(clock, lambda provider, model, state: states.append((provider, model, state)))
    breaker = board.get("platform", "m1")
    assert board.get("platform", "m1") is breaker
    assert board.get("platform", "m2") is not breaker
    assert board.get("org:x", "m1") is not breaker
    _fail(breaker, 5)
    assert states == [
        ("platform", "m1", "closed"),
        ("platform", "m2", "closed"),
        ("org:x", "m1", "closed"),
        ("platform", "m1", "open"),
    ]


def test_transitions_are_logged() -> None:
    stream = json_log_stream()
    board = BreakerBoard(FrozenClock())
    _fail(board.get("platform", "m1"), 5)
    [event] = [e for e in log_events(stream) if e["event"] == "llm.breaker_transition"]
    assert (event["provider"], event["model"], event["from"], event["to"]) == (
        "platform",
        "m1",
        "closed",
        "open",
    )


def test_board_without_a_state_hook() -> None:
    board = BreakerBoard(FrozenClock())
    breaker = board.get("platform", "m1")
    _fail(breaker, 5)
    assert _state(breaker) == "open"
