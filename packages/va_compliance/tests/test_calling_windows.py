"""Callee timezones and window checks (specs/17 §3.1-§3.2)."""

from __future__ import annotations

from datetime import UTC, datetime

import phonenumbers
import pytest
from phonenumbers import timezone as number_timezones

from va_compliance.calling_windows import (
    MAX_CANDIDATE_ZONES,
    callee_timezone,
    callee_timezones,
    in_window,
    local_minute,
)
from va_domain.scheduling import ALL_DAYS, CallingWindow

DAILY = [CallingWindow(days=list(ALL_DAYS), start="08:00", end="21:00")]
WEEKDAYS = [CallingWindow(days=["mon", "tue", "wed", "thu", "fri"], start="09:00", end="17:00")]


def utc(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _multi_zone_npa() -> str:
    for area in range(200, 1000):
        zones = number_timezones.time_zones_for_number(phonenumbers.parse(f"+1{area}5550123"))
        if 1 < len(zones) <= MAX_CANDIDATE_ZONES:
            return f"+1{area}5550123"
    pytest.fail("phonenumbers maps no NANP area code to several zones")


def test_single_zone_number() -> None:
    assert callee_timezones("+12125550123", explicit=None, fallback="UTC") == ("America/New_York",)
    assert callee_timezone("+12125550123", explicit=None, fallback="UTC") == "America/New_York"


def test_multi_zone_number_returns_every_zone_sorted() -> None:
    zones = callee_timezones(_multi_zone_npa(), explicit=None, fallback="UTC")
    assert len(zones) > 1
    assert list(zones) == sorted(zones)
    assert callee_timezone(_multi_zone_npa(), explicit=None, fallback="UTC") == zones[0]


@pytest.mark.parametrize(
    "e164",
    [
        "+80012345678",  # international freephone: Etc/Unknown
        "+15005550123",  # NANP personal number: every NANP zone
        "not a number",
    ],
)
def test_unknown_or_uninformative_zones_use_the_fallback(e164: str) -> None:
    assert callee_timezones(e164, explicit=None, fallback="America/Chicago") == ("America/Chicago",)


def test_explicit_zone_wins() -> None:
    assert callee_timezones("+12125550123", explicit="Asia/Tokyo", fallback="UTC") == ("Asia/Tokyo",)


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        (utc(2026, 7, 15, 12, 0), True),  # 08:00 EDT
        (utc(2026, 7, 15, 11, 59), False),  # 07:59
        (utc(2026, 7, 16, 0, 59), True),  # 20:59
        (utc(2026, 7, 16, 1, 0), False),  # 21:00, end is exclusive
    ],
)
def test_window_edges_in_new_york(instant: datetime, expected: bool) -> None:
    assert in_window(instant, "America/New_York", DAILY) is expected


def test_spring_forward_in_new_york() -> None:
    # 12:30 UTC is 07:30 EST on 2026-03-07 and 08:30 EDT on 2026-03-08 (the switch day).
    assert not in_window(utc(2026, 3, 7, 12, 30), "America/New_York", DAILY)
    assert in_window(utc(2026, 3, 8, 12, 30), "America/New_York", DAILY)


def test_fall_back_in_new_york() -> None:
    # 2026-11-01: the same 12:30 UTC is 08:30 EDT on Saturday and 07:30 EST on Sunday.
    assert in_window(utc(2026, 10, 31, 12, 30), "America/New_York", DAILY)
    assert not in_window(utc(2026, 11, 1, 12, 30), "America/New_York", DAILY)


def test_london_summer_time() -> None:
    assert not in_window(utc(2026, 3, 28, 7, 30), "Europe/London", DAILY)  # 07:30 GMT
    assert in_window(utc(2026, 3, 29, 7, 30), "Europe/London", DAILY)  # 08:30 BST


def test_lord_howe_half_hour_dst() -> None:
    # Lord Howe moves from UTC+10:30 to UTC+11 on 2026-10-04.
    assert local_minute(utc(2026, 10, 2, 21, 0), "Australia/Lord_Howe") == ("sat", 7 * 60 + 30)
    assert local_minute(utc(2026, 10, 4, 21, 0), "Australia/Lord_Howe") == ("mon", 8 * 60)
    assert not in_window(utc(2026, 10, 2, 21, 0), "Australia/Lord_Howe", DAILY)
    assert in_window(utc(2026, 10, 4, 21, 0), "Australia/Lord_Howe", DAILY)


def test_weekday_limited_window() -> None:
    assert in_window(utc(2026, 11, 6, 15, 0), "UTC", WEEKDAYS)  # Friday 15:00
    assert not in_window(utc(2026, 11, 7, 15, 0), "UTC", WEEKDAYS)  # Saturday


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        local_minute(datetime(2026, 1, 1, 12, 0), "UTC")  # noqa: DTZ001 - the error under test
