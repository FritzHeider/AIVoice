"""Callback rules of the ``schedule_callback`` tool (specs/13 §6 Decision P08-T02b2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import JsonValue

from va_domain.call_callback import CALLBACK_NOTE_KEY, CallbackTimeError, callback_config, callback_time
from va_domain.call_retry import RETRY_ATTEMPT_KEY
from va_domain.numbers import MAINTENANCE_MESSAGE_KEY

NOW = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)
LA = "America/Los_Angeles"


def test_an_offset_is_kept() -> None:
    assert callback_time("2026-11-06T09:30:00-05:00", LA, NOW) == datetime(2026, 11, 6, 14, 30, tzinfo=UTC)


def test_the_send_call_offset_form_is_accepted() -> None:
    assert callback_time("2026-11-06 09:30:00 +01:00", LA, NOW) == datetime(2026, 11, 6, 8, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("timezone", "expected"),
    [
        (LA, datetime(2026, 11, 6, 17, 30, tzinfo=UTC)),
        ("America/New_York", datetime(2026, 11, 6, 14, 30, tzinfo=UTC)),
    ],
)
def test_a_naive_time_is_localized_in_the_call_timezone(timezone: str, expected: datetime) -> None:
    assert callback_time("2026-11-06T09:30:00", timezone, NOW) == expected


def test_an_ambiguous_local_time_takes_the_first_occurrence() -> None:
    now = datetime(2026, 10, 30, tzinfo=UTC)
    # 2026-11-01 01:30 happens twice in Los Angeles; fold=0 is the earlier one (PDT, UTC-7).
    assert callback_time("2026-11-01T01:30:00", LA, now) == datetime(2026, 11, 1, 8, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("tomorrow at nine", "invalid_datetime"),
        ("", "invalid_datetime"),
        ("2026-11-05T15:00:00Z", "in_the_past"),
        ("2026-11-04T10:00:00Z", "in_the_past"),
        ((NOW + timedelta(days=90, seconds=1)).isoformat(), "too_far_ahead"),
    ],
)
def test_unusable_times_are_refused_with_a_reason(value: str, reason: str) -> None:
    with pytest.raises(CallbackTimeError) as caught:
        callback_time(value, LA, NOW)
    assert caught.value.reason == reason


def test_exactly_ninety_days_ahead_is_allowed() -> None:
    moment = NOW + timedelta(days=90)
    assert callback_time(moment.isoformat(), LA, NOW) == moment


def test_the_child_config_is_the_parents_with_the_callback_start_time() -> None:
    stored: dict[str, JsonValue] = {
        "task": "Sell",
        "timezone": LA,
        "start_time": None,
        RETRY_ATTEMPT_KEY: 2,
        MAINTENANCE_MESSAGE_KEY: "Closed.",
        "pricing": {"per_minute": 1},
    }
    config = callback_config(stored, datetime(2026, 11, 6, 17, 30, tzinfo=UTC), "  Wants the blue plan  ")
    assert config == {
        "task": "Sell",
        "timezone": LA,
        "start_time": "2026-11-06T17:30:00Z",
        "pricing": {"per_minute": 1},
        CALLBACK_NOTE_KEY: "Wants the blue plan",
    }
    assert stored[RETRY_ATTEMPT_KEY] == 2
    assert stored["start_time"] is None


@pytest.mark.parametrize("note", [None, "", "   "])
def test_a_blank_note_is_not_stored(note: str | None) -> None:
    stored: dict[str, JsonValue] = {"task": "Sell", CALLBACK_NOTE_KEY: "old"}
    config = callback_config(stored, datetime(2026, 11, 6, tzinfo=UTC), note)
    assert config == {"task": "Sell", "start_time": "2026-11-06T00:00:00Z"}
