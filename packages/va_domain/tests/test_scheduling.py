"""`CallingWindow` and the overlap check (specs/17-batches-and-scheduling.md §3)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from va_domain.scheduling import ALL_DAYS, CallingWindow, unique_items, windows_overlap


def _window(start: str, end: str, *days: str) -> CallingWindow:
    return CallingWindow.model_validate({"days": list(days), "start": start, "end": end})


def test_a_valid_window() -> None:
    window = _window("00:00", "23:59", *ALL_DAYS)
    assert window.days == list(ALL_DAYS)


@pytest.mark.parametrize(
    ("payload", "location"),
    [
        ({"days": [], "start": "08:00", "end": "09:00"}, ("days",)),
        ({"days": ["mon", "mon"], "start": "08:00", "end": "09:00"}, ("days",)),
        ({"days": ["monday"], "start": "08:00", "end": "09:00"}, ("days", 0)),
        ({"days": ["mon"], "start": "8:00", "end": "09:00"}, ("start",)),
        ({"days": ["mon"], "start": "08:00", "end": "09:60"}, ("end",)),
        ({"days": ["mon"], "start": "09:00", "end": "09:00"}, ()),
        ({"days": ["mon"], "start": "22:00", "end": "06:00"}, ()),
        ({"days": ["mon"], "start": "08:00", "end": "09:00", "timezone": "UTC"}, ("timezone",)),
    ],
)
def test_invalid_windows(payload: dict[str, Any], location: tuple[str | int, ...]) -> None:
    with pytest.raises(ValidationError) as caught:
        CallingWindow.model_validate(payload)
    assert [error["loc"] for error in caught.value.errors()] == [location]


def test_windows_on_different_days_never_overlap() -> None:
    assert not windows_overlap([_window("08:00", "12:00", "mon"), _window("09:00", "13:00", "tue")])


def test_touching_windows_do_not_overlap() -> None:
    assert not windows_overlap([_window("12:00", "17:00", "mon"), _window("08:00", "12:00", "mon", "tue")])


def test_intersecting_windows_on_a_shared_day_overlap() -> None:
    assert windows_overlap([_window("08:00", "12:00", "mon", "wed"), _window("11:59", "13:00", "wed")])


def test_a_window_inside_another_overlaps() -> None:
    assert windows_overlap([_window("08:00", "20:00", "sun"), _window("10:00", "11:00", "sun")])


def test_no_windows_do_not_overlap() -> None:
    assert not windows_overlap([])


def test_unique_items() -> None:
    assert unique_items([1, 2, 3]) == [1, 2, 3]
    with pytest.raises(ValueError, match="duplicate item 'a'"):
        unique_items(["a", "b", "a"])
