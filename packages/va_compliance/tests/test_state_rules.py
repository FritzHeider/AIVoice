"""State calling rules table (specs/17 §7, Decision P05-T04a)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from va_compliance.state_rules import STATE_CALLING_HOURS, StateRule, state_allows, state_rules

STATES = {"FL", "OK", "MD", "WA", "TX", "AL", "LA", "MS", "UT"}


def minute(clock: str) -> int:
    hours, minutes = clock.split(":")
    return int(hours) * 60 + int(minutes)


def test_table_has_exactly_the_spec_states() -> None:
    assert set(state_rules()) == STATES
    assert STATE_CALLING_HOURS is state_rules()


@pytest.mark.parametrize("state", sorted(STATES))
def test_every_entry_cites_its_statute(state: str) -> None:
    rule = state_rules()[state]
    assert rule.statute.strip()
    assert rule.source_url.startswith("https://")
    assert rule.last_verified <= datetime.now(UTC).date()


def test_frequency_caps() -> None:
    caps = {state: rule.max_calls_per_24h for state, rule in state_rules().items() if rule.max_calls_per_24h}
    assert caps == {"FL": 3, "OK": 3, "MD": 3}


@pytest.mark.parametrize(
    ("state", "day", "clock", "expected"),
    [
        ("FL", "sun", "08:00", True),
        ("FL", "mon", "19:59", True),
        ("FL", "mon", "20:00", False),
        ("OK", "wed", "07:59", False),
        ("WA", "sat", "20:00", False),
        ("TX", "sun", "11:59", False),
        ("TX", "sun", "12:00", True),
        ("TX", "mon", "09:00", True),
        ("TX", "mon", "08:59", False),
        ("TX", "sat", "20:59", True),
        ("AL", "sun", "12:00", False),
        ("AL", "mon", "12:00", True),
        ("LA", "sun", "12:00", False),
        ("MS", "sun", "12:00", False),
        ("MS", "mon", "06:00", True),
        ("UT", "mon", "20:30", True),
        ("UT", "sun", "12:00", False),
        ("MD", "mon", "20:30", False),
    ],
)
def test_state_allows(state: str, day: str, clock: str, expected: bool) -> None:
    assert state_allows(state_rules()[state], day, minute(clock)) is expected  # type: ignore[arg-type]


def test_rule_without_windows_or_days_never_restricts() -> None:
    rule = StateRule(statute="x", source_url="https://law.example.test/x", last_verified="2026-11-01")  # type: ignore[arg-type]
    assert state_allows(rule, "sun", 0)
    assert state_allows(rule, "wed", 23 * 60 + 59)


@pytest.mark.parametrize("url", ["http://law.example.test/x", "https://"])
def test_source_url_must_be_https(url: str) -> None:
    with pytest.raises(ValidationError, match="https"):
        StateRule(statute="x", source_url=url, last_verified="2026-11-01")  # type: ignore[arg-type]
