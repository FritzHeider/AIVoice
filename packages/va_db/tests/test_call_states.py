"""Call and leg state machines against specs/05-calls-api.md §8, §9 (and the §8 Decision P05-T02)."""

from __future__ import annotations

import itertools
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import get_args

import pytest
from model_introspection import SPEC_03

from va_db.call_states import (
    CALL_TRANSITIONS,
    END_REASONS,
    LEG_TRANSITIONS,
    STOPPABLE_STATUSES,
    TERMINAL_STATUSES,
    EndReason,
    allowed_from,
    call_durations,
    is_terminal,
    leg_allowed_from,
)
from va_db.models import CallLegState, CallStatus

SPEC_05 = SPEC_03.with_name("05-calls-api.md")

DIAGRAM_EDGES: frozenset[tuple[CallStatus, CallStatus]] = frozenset(
    {
        ("scheduled", "queued"),
        ("scheduled", "canceled"),
        ("queued", "dialing"),
        ("queued", "failed"),
        ("queued", "canceled"),
        ("dialing", "ringing"),
        ("dialing", "failed"),
        ("ringing", "in_progress"),
        ("ringing", "no_answer"),
        ("ringing", "busy"),
        ("ringing", "failed"),
        ("in_progress", "transferring"),
        ("transferring", "transferred"),
        ("transferred", "completed"),
        ("in_progress", "completed"),
    }
)
"""Every arrow of the specs/05 §8 diagram."""

REORDER_EDGES: frozenset[tuple[CallStatus, CallStatus]] = frozenset(
    {
        ("dialing", "in_progress"),
        ("dialing", "no_answer"),
        ("dialing", "busy"),
        ("dialing", "canceled"),
        ("ringing", "canceled"),
        ("transferring", "completed"),
    }
)
"""The carrier-reorder tolerance added by the §8 Decision P05-T02."""

DISPATCH_EDGES: frozenset[tuple[CallStatus, CallStatus]] = frozenset({("scheduled", "failed")})
"""A scheduled call whose workflow could not be started (specs/05 §5 Decision P05-T05b)."""


def _spec_end_reasons() -> list[tuple[str, str]]:
    text = SPEC_05.read_text(encoding="utf-8")
    section = text.split("## 9. `end_reason` enumeration", 1)[1].split("\n## ", 1)[0]
    return re.findall(r"^\| `([a-z_]+)` \| ([a-z_]+) \|", section, flags=re.MULTILINE)


def test_end_reasons_equal_the_spec_table_in_order() -> None:
    rows = _spec_end_reasons()
    assert len(rows) == 26
    assert list(END_REASONS.items()) == rows
    assert list(get_args(EndReason)) == [reason for reason, _ in rows]


@pytest.mark.parametrize(("source", "target"), list(itertools.product(get_args(CallStatus), repeat=2)))
def test_call_transition_matrix(source: CallStatus, target: CallStatus) -> None:
    allowed = (source, target) in DIAGRAM_EDGES | REORDER_EDGES | DISPATCH_EDGES
    assert (source in allowed_from(target)) is allowed


def test_terminal_statuses_have_no_outgoing_transition() -> None:
    sources = set().union(*CALL_TRANSITIONS.values())
    assert {"completed", "failed", "canceled", "no_answer", "busy"} == TERMINAL_STATUSES
    assert not sources & TERMINAL_STATUSES
    assert all(is_terminal(status) for status in TERMINAL_STATUSES)
    assert not any(is_terminal(status) for status in sources)
    assert set(CALL_TRANSITIONS) == set(get_args(CallStatus))


def test_stoppable_statuses_are_every_live_status() -> None:
    assert set(get_args(CallStatus)) - STOPPABLE_STATUSES == TERMINAL_STATUSES | {"transferred"}
    assert not STOPPABLE_STATUSES & TERMINAL_STATUSES


LEG_EDGES: frozenset[tuple[CallLegState, CallLegState]] = frozenset(
    {
        ("initiated", "ringing"),
        ("initiated", "answered"),
        ("ringing", "answered"),
        ("answered", "bridged"),
        ("initiated", "bridged"),
        ("ringing", "bridged"),
        ("initiated", "hangup"),
        ("ringing", "hangup"),
        ("answered", "hangup"),
        ("bridged", "hangup"),
    }
)


@pytest.mark.parametrize(("source", "target"), list(itertools.product(get_args(CallLegState), repeat=2)))
def test_leg_transition_matrix(source: CallLegState, target: CallLegState) -> None:
    assert (source in leg_allowed_from(target)) is ((source, target) in LEG_EDGES)


def test_leg_hangup_is_final_and_initiated_is_never_entered() -> None:
    assert set(LEG_TRANSITIONS) == set(get_args(CallLegState))
    assert not leg_allowed_from("initiated")
    assert "hangup" not in set().union(*LEG_TRANSITIONS.values())


_ANSWERED = datetime(2026, 11, 1, 12, tzinfo=UTC)


def test_connected_seconds_run_from_answer_to_end_without_a_transfer() -> None:
    ended = _ANSWERED + timedelta(seconds=61, microseconds=234_567)

    assert call_durations(_ANSWERED, ended) == {"connected_seconds": Decimal("61.235")}


def test_a_transfer_splits_connected_and_transfer_seconds() -> None:
    transfer = _ANSWERED + timedelta(seconds=30)
    ended = _ANSWERED + timedelta(seconds=95, milliseconds=500)

    assert call_durations(_ANSWERED, ended, transfer) == {
        "transfer_seconds": Decimal("65.500"),
        "connected_seconds": Decimal("30.000"),
    }


def test_a_transfer_started_after_the_end_counts_connected_time_to_the_end() -> None:
    ended = _ANSWERED + timedelta(seconds=10)

    assert call_durations(_ANSWERED, ended, ended + timedelta(seconds=2)) == {
        "transfer_seconds": Decimal("0.000"),
        "connected_seconds": Decimal("10.000"),
    }


def test_an_end_before_the_answer_clamps_to_zero() -> None:
    assert call_durations(_ANSWERED, _ANSWERED - timedelta(seconds=3)) == {
        "connected_seconds": Decimal("0.000")
    }
