"""Billing-period arithmetic for FakeStripe subscriptions (specs/30 §4.1 Decision P03-T07b rules 5, 7).

Times are unix seconds in UTC. Months and years are calendar steps that keep the billing anchor day and clamp
to the month end (anchor 31: Jan 31 → Feb 28 → Mar 31). Prorations round half up.
"""

from __future__ import annotations

import calendar
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from types import MappingProxyType
from typing import Final, Literal

__all__ = ["INTERVALS", "MAX_INTERVAL_COUNT", "Interval", "add_interval", "anchor_day", "prorate"]

type Interval = Literal["day", "week", "month", "year"]

INTERVALS: Final[tuple[Interval, ...]] = ("day", "week", "month", "year")
# Stripe's limit: at most one year per period.
MAX_INTERVAL_COUNT: Final[Mapping[str, int]] = MappingProxyType(
    {"day": 365, "week": 52, "month": 12, "year": 1}
)
_DAY: Final = 86_400
_WEEK: Final = 7 * _DAY
_MONTHS_PER_YEAR: Final = 12


def anchor_day(timestamp: int) -> int:
    """The UTC day of month of ``timestamp``."""
    return datetime.fromtimestamp(timestamp, UTC).day


def add_interval(timestamp: int, interval: str, count: int, *, anchor: int | None = None) -> int:
    """``timestamp`` plus ``count`` ``interval``s; month/year steps land on day ``anchor`` (default: the
    day of ``timestamp``) clamped to the month end."""
    if interval == "day":
        return timestamp + count * _DAY
    if interval == "week":
        return timestamp + count * _WEEK
    months = count * (_MONTHS_PER_YEAR if interval == "year" else 1)
    start = datetime.fromtimestamp(timestamp, UTC)
    year, month_index = divmod(start.month - 1 + months, _MONTHS_PER_YEAR)
    year += start.year
    month = month_index + 1
    day = min(anchor if anchor is not None else start.day, calendar.monthrange(year, month)[1])
    return int(start.replace(year=year, month=month, day=day).timestamp())


def prorate(amount: int, *, start: int, end: int, now: int) -> int:
    """``amount`` times the unused share of ``[start, end)`` at ``now`` (clamped into the period), half up."""
    if end <= start:
        return 0
    remaining = end - min(max(now, start), end)
    share = Decimal(amount) * Decimal(remaining) / Decimal(end - start)
    return int(share.quantize(Decimal(1), rounding=ROUND_HALF_UP))
