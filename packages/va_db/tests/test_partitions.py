"""Pure call_events partition planning: names, UTC month bounds, expiry (specs/03 §10 Decision)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from va_common.time import FrozenClock
from va_db.partitions import (
    MonthPartition,
    call_event_partition,
    expired_call_event_partitions,
    month_start,
    partition_month,
    planned_call_event_partitions,
)


def _utc(year: int, month: int, day: int = 1) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


def test_planned_partitions_cover_the_current_utc_month_and_three_more() -> None:
    now = FrozenClock(datetime(2026, 11, 15, 12, tzinfo=UTC)).now()
    assert planned_call_event_partitions(now) == (
        MonthPartition("call_events_p202611", _utc(2026, 11), _utc(2026, 12)),
        MonthPartition("call_events_p202612", _utc(2026, 12), _utc(2027, 1)),
        MonthPartition("call_events_p202701", _utc(2027, 1), _utc(2027, 2)),
        MonthPartition("call_events_p202702", _utc(2027, 2), _utc(2027, 3)),
    )


def test_planned_partitions_roll_over_the_year() -> None:
    now = datetime(2026, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)
    names = [partition.name for partition in planned_call_event_partitions(now)]
    assert names == [
        "call_events_p202612",
        "call_events_p202701",
        "call_events_p202702",
        "call_events_p202703",
    ]


def test_non_utc_input_is_normalised_to_the_utc_month() -> None:
    evening_in_new_york = datetime(2026, 10, 31, 20, 30, tzinfo=timezone(timedelta(hours=-5)))
    assert planned_call_event_partitions(evening_in_new_york, 0) == (
        MonthPartition("call_events_p202611", _utc(2026, 11), _utc(2026, 12)),
    )
    assert call_event_partition(evening_in_new_york).name == "call_events_p202611"


def test_month_start_shifts_backwards_and_forwards() -> None:
    moment = datetime(2026, 1, 20, 8, tzinfo=UTC)
    assert [month_start(moment, offset) for offset in (-1, 0, 12)] == [
        _utc(2025, 12),
        _utc(2026, 1),
        _utc(2027, 1),
    ]


def test_naive_datetimes_are_rejected() -> None:
    naive = datetime(2026, 11, 15, 12)  # noqa: DTZ001  (the rejected input)
    with pytest.raises(ValueError, match="naive datetime"):
        planned_call_event_partitions(naive)
    with pytest.raises(ValueError, match="naive datetime"):
        expired_call_event_partitions(["call_events_p202611"], naive)


def test_months_ahead_bounds() -> None:
    now = _utc(2026, 11, 15)
    assert len(planned_call_event_partitions(now, 0)) == 1
    with pytest.raises(ValueError, match="must not be negative"):
        planned_call_event_partitions(now, -1)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("call_events_p202602", _utc(2026, 2)),
        ("call_events_p202612", _utc(2026, 12)),
        ("call_events_p202613", None),
        ("call_events_p202600", None),
        ("call_events_p000001", None),
        ("call_events_p2026011", None),
        ("call_events_2026_10", None),
        ("call_events_legacy", None),
        ("calls_p202611", None),
    ],
)
def test_partition_month_parses_only_the_partition_naming_rule(name: str, expected: datetime | None) -> None:
    assert partition_month(name) == expected


def test_expired_partitions_end_at_or_before_the_cutoff_in_month_order() -> None:
    names = ["call_events_p202612", "call_events_p202610", "call_events_legacy", "call_events_p202611"]
    cutoff = _utc(2026, 12)
    assert expired_call_event_partitions(names, cutoff) == ("call_events_p202610", "call_events_p202611")
    assert expired_call_event_partitions(names, cutoff - timedelta(microseconds=1)) == (
        "call_events_p202610",
    )
    assert expired_call_event_partitions(names, _utc(2026, 10)) == ()
