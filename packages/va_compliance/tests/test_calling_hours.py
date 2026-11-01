"""Single-call calling hours with state rules (specs/17 §7, specs/23 §3)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import phonenumbers
import pytest
from phonenumbers import timezone as number_timezones

from va_compliance.calling_hours import calling_hours_open, destination_state_rule
from va_compliance.state_rules import state_rules
from va_domain.org import load_settings

FLORIDA = "+13055550123"  # America/New_York
TEXAS = "+12145550123"  # America/Chicago
NEW_YORK = "+12125550123"
LONDON = "+442071234567"
IDAHO = "+12085550123"  # America/Boise and America/Los_Angeles


def utc(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def open_at(at: datetime, e164: str, **compliance: Any) -> bool:
    return calling_hours_open(at, e164, settings=load_settings({"compliance": compliance}))


def test_destination_state_rule() -> None:
    assert destination_state_rule(FLORIDA) is state_rules()["FL"]
    assert destination_state_rule(NEW_YORK) is None
    assert destination_state_rule(LONDON) is None
    assert destination_state_rule("+18005550123") is None


def test_florida_state_hours_apply_on_top_of_org_hours() -> None:
    evening = utc(2026, 7, 16, 0, 30)  # 20:30 EDT on a Wednesday
    assert not open_at(evening, FLORIDA)
    assert open_at(evening, FLORIDA, state_quiet_hours=False)
    assert open_at(evening, NEW_YORK)


def test_texas_sunday_morning_is_closed() -> None:
    sunday_ten = utc(2026, 7, 19, 15, 0)  # 10:00 CDT
    assert not open_at(sunday_ten, TEXAS)
    assert open_at(sunday_ten, TEXAS, state_quiet_hours=False)


def test_custom_org_windows() -> None:
    windows = [{"days": ["mon"], "start": "10:00", "end": "11:00"}]
    monday = utc(2026, 7, 13, 14, 30)  # 10:30 EDT
    assert open_at(monday, NEW_YORK, calling_hours=windows)
    assert not open_at(utc(2026, 7, 14, 14, 30), NEW_YORK, calling_hours=windows)  # Tuesday


def test_multi_zone_number_must_be_open_in_every_zone() -> None:
    zones = number_timezones.time_zones_for_number(phonenumbers.parse(IDAHO))
    assert set(zones) == {"America/Boise", "America/Los_Angeles"}
    # 15:30 UTC = 09:30 MDT (Boise) and 08:30 PDT (Los Angeles).
    windows = [{"days": ["wed"], "start": "09:00", "end": "17:00"}]
    assert not open_at(utc(2026, 7, 15, 15, 30), IDAHO, calling_hours=windows)
    assert open_at(utc(2026, 7, 15, 16, 30), IDAHO, calling_hours=windows)


@pytest.mark.parametrize(("hour", "expected"), [(7, False), (8, True), (20, True), (21, False)])
def test_non_nanp_destination_uses_org_windows_only(hour: int, expected: bool) -> None:
    assert open_at(utc(2026, 1, 11, hour, 0), LONDON) is expected  # Sunday, GMT
