"""Calling windows shared by batch schedules and org calling hours (specs/17-batches-and-scheduling.md §3)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from itertools import pairwise
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

__all__ = ["ALL_DAYS", "CallingWindow", "Weekday", "unique_items", "windows_overlap"]

type Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

ALL_DAYS: tuple[Weekday, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

_CLOCK_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


def unique_items[T](items: list[T]) -> list[T]:
    """Reject a list that repeats an item (pydantic v2 has no `unique_items` constraint)."""
    seen: list[T] = []
    for item in items:
        if item in seen:
            msg = f"duplicate item {item!r}"
            raise ValueError(msg)
        seen.append(item)
    return items


class CallingWindow(BaseModel):
    """One daily window, local to the callee. Overnight windows are split into two windows."""

    model_config = ConfigDict(extra="forbid")

    days: Annotated[list[Weekday], AfterValidator(unique_items)] = Field(
        min_length=1, max_length=7, description="Days the window applies to; at least one, no repeats."
    )
    start: str = Field(pattern=_CLOCK_PATTERN, description='Opening time, "HH:MM" 24 h.')
    end: str = Field(pattern=_CLOCK_PATTERN, description='Closing time, "HH:MM" 24 h, later than `start`.')

    @model_validator(mode="after")
    def _end_after_start(self) -> Self:
        # Zero-padded "HH:MM" strings order the same way as the times they spell.
        if self.end <= self.start:
            msg = "end must be later than start (split overnight windows into two windows)"
            raise ValueError(msg)
        return self


def windows_overlap(windows: Iterable[CallingWindow]) -> bool:
    """True when two windows share a day and their time ranges intersect (touching ends do not overlap)."""
    by_day: defaultdict[str, list[tuple[str, str]]] = defaultdict(list)
    for window in windows:
        for day in window.days:
            by_day[day].append((window.start, window.end))
    for ranges in by_day.values():
        ordered = sorted(ranges)
        if any(later[0] < earlier[1] for earlier, later in pairwise(ordered)):
            return True
    return False
