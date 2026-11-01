"""Callee timezones and calling-window checks (specs/17-batches-and-scheduling.md §3.1-§3.2).

A number may map to several zones (a US area code spanning two zones); callers evaluate the window in every
candidate zone and treat the number as open only when it is open in all of them (§3.1 step 3).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Final
from zoneinfo import ZoneInfo

import phonenumbers
from phonenumbers import timezone as number_timezones

from va_domain.scheduling import ALL_DAYS, CallingWindow, Weekday

__all__ = [
    "MAX_CANDIDATE_ZONES",
    "callee_timezone",
    "callee_timezones",
    "in_window",
    "local_minute",
    "minute_in_windows",
]

_UNKNOWN_ZONE: Final = "Etc/Unknown"

# More zones than this means phonenumbers only knows the calling code (e.g. every NANP zone for a `+1 500`
# personal number); intersecting them would close the window for good, so the org fallback is used instead.
MAX_CANDIDATE_ZONES: Final = 8


def _minutes(clock: str) -> int:
    hours, minutes = clock.split(":")
    return int(hours) * 60 + int(minutes)


def callee_timezones(e164: str, *, explicit: str | None, fallback: str) -> tuple[str, ...]:
    """Every candidate IANA zone of `e164`, sorted: `(explicit,)` when given, `(fallback,)` when unknown."""
    if explicit is not None:
        return (explicit,)
    try:
        parsed = phonenumbers.parse(e164, None)
    except phonenumbers.NumberParseException:
        return (fallback,)
    zones = tuple(zone for zone in number_timezones.time_zones_for_number(parsed) if zone != _UNKNOWN_ZONE)
    if not zones or len(zones) > MAX_CANDIDATE_ZONES:
        return (fallback,)
    return tuple(sorted(zones))


def callee_timezone(e164: str, *, explicit: str | None, fallback: str) -> str:
    """One representative zone (the first candidate); window checks must use :func:`callee_timezones`."""
    return callee_timezones(e164, explicit=explicit, fallback=fallback)[0]


def local_minute(now_utc: datetime, tz: str) -> tuple[Weekday, int]:
    """The weekday and minute of the day (0-1439) of `now_utc` in zone `tz`."""
    if now_utc.tzinfo is None:
        msg = "now_utc must be timezone-aware"
        raise ValueError(msg)
    local = now_utc.astimezone(ZoneInfo(tz))
    return ALL_DAYS[local.weekday()], local.hour * 60 + local.minute


def minute_in_windows(weekday: Weekday, minute: int, windows: Sequence[CallingWindow]) -> bool:
    """Whether `minute` of `weekday` falls in one of `windows` (`[start, end)`)."""
    return any(
        weekday in window.days and _minutes(window.start) <= minute < _minutes(window.end)
        for window in windows
    )


def in_window(now_utc: datetime, tz: str, windows: Sequence[CallingWindow]) -> bool:
    """Whether the local time of `now_utc` in `tz` falls inside one of `windows`."""
    weekday, minute = local_minute(now_utc, tz)
    return minute_in_windows(weekday, minute, windows)
