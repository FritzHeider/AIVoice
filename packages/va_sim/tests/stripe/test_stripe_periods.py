"""Billing-period arithmetic (specs/30 §4.1 Decision P03-T07b rules 5, 7)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from va_sim.stripe.periods import MAX_INTERVAL_COUNT, add_interval, anchor_day, prorate


def ts(year: int, month: int, day: int, hour: int = 0) -> int:
    return int(datetime(year, month, day, hour, tzinfo=UTC).timestamp())


def test_months_keep_the_anchor_day_and_clamp_to_the_month_end() -> None:
    start = ts(2026, 1, 31, 9)
    february = add_interval(start, "month", 1)
    march = add_interval(february, "month", 1, anchor=anchor_day(start))
    leap = add_interval(ts(2028, 1, 31), "month", 1)
    assert february == ts(2026, 2, 28, 9)
    assert march == ts(2026, 3, 31, 9)
    assert leap == ts(2028, 2, 29)
    assert add_interval(ts(2026, 11, 15), "month", 3) == ts(2027, 2, 15)
    assert add_interval(ts(2026, 12, 1), "month", 12) == ts(2027, 12, 1)


def test_years_weeks_and_days() -> None:
    assert add_interval(ts(2028, 2, 29), "year", 1) == ts(2029, 2, 28)
    assert add_interval(ts(2026, 3, 1), "week", 2) == ts(2026, 3, 15)
    assert add_interval(ts(2026, 3, 1), "day", 30) == ts(2026, 3, 31)


def test_interval_count_limits_are_one_year() -> None:
    assert dict(MAX_INTERVAL_COUNT) == {"day": 365, "week": 52, "month": 12, "year": 1}


@pytest.mark.parametrize(
    ("now", "expected"),
    [(0, 1000), (50, 500), (75, 250), (100, 0), (-10, 1000), (150, 0), (1, 990)],
)
def test_prorate_is_the_unused_share_clamped_into_the_period(now: int, expected: int) -> None:
    assert prorate(1000, start=0, end=100, now=now) == expected


def test_prorate_rounds_half_up_and_ignores_empty_periods() -> None:
    assert prorate(1, start=0, end=2, now=1) == 1
    assert prorate(3, start=0, end=2, now=1) == 2
    assert prorate(1000, start=5, end=5, now=5) == 0
