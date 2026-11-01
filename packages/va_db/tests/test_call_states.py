"""Call and leg state machines against specs/05-calls-api.md §8, §9 (and the §8 Decision P05-T02)."""

from __future__ import annotations

import itertools
import re
from typing import get_args

import pytest
from model_introspection import SPEC_03

from va_db.call_states import (
    CALL_TRANSITIONS,
    END_REASONS,
    LEG_TRANSITIONS,
    TERMINAL_STATUSES,
    EndReason,
    allowed_from,
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
    allowed = (source, target) in DIAGRAM_EDGES | REORDER_EDGES
    assert (source in allowed_from(target)) is allowed


def test_terminal_statuses_have_no_outgoing_transition() -> None:
    sources = set().union(*CALL_TRANSITIONS.values())
    assert {"completed", "failed", "canceled", "no_answer", "busy"} == TERMINAL_STATUSES
    assert not sources & TERMINAL_STATUSES
    assert all(is_terminal(status) for status in TERMINAL_STATUSES)
    assert not any(is_terminal(status) for status in sources)
    assert set(CALL_TRANSITIONS) == set(get_args(CallStatus))


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
