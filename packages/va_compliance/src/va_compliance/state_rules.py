"""State-specific calling rules applied on top of org calling hours (specs/17-batches-and-scheduling.md §7).

The table is data (`data/state_rules.json`), keyed by USPS state code. An empty `windows` list puts no limit
on the hours; `no_call_days` forbids calling on those weekdays; `max_calls_per_24h` is the per-number
frequency cap that `va_billing` enforces with `freq:` counters. Every entry cites its statute (specs/17 §7
Decision P05-T04a).
"""

from __future__ import annotations

import functools
from collections.abc import Mapping
from datetime import date
from importlib import resources
from types import MappingProxyType
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter

from va_compliance.calling_windows import minute_in_windows
from va_domain.scheduling import CallingWindow, Weekday, unique_items

__all__ = ["STATE_CALLING_HOURS", "StateRule", "state_allows", "state_rules"]


def _https(url: str) -> str:
    if not url.startswith("https://") or len(url) <= len("https://"):
        msg = "source_url must be an https URL"
        raise ValueError(msg)
    return url


class StateRule(BaseModel):
    """One state's calling rule and the statute it implements."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    windows: list[CallingWindow] = Field(default_factory=list)
    no_call_days: Annotated[list[Weekday], AfterValidator(unique_items)] = Field(default_factory=list)
    max_calls_per_24h: int | None = Field(default=None, ge=1)
    statute: str = Field(min_length=1)
    source_url: Annotated[str, AfterValidator(_https)]
    last_verified: date


_TABLE = TypeAdapter(dict[str, StateRule])


@functools.cache
def state_rules() -> Mapping[str, StateRule]:
    """The parsed table (read once per process)."""
    raw = resources.files("va_compliance").joinpath("data", "state_rules.json").read_text(encoding="utf-8")
    return MappingProxyType(_TABLE.validate_json(raw))


STATE_CALLING_HOURS: Mapping[str, StateRule] = state_rules()


def state_allows(rule: StateRule, weekday: Weekday, minute: int) -> bool:
    """Whether `rule` permits a call at local `minute` (0-1439) of `weekday`."""
    if weekday in rule.no_call_days:
        return False
    return not rule.windows or minute_in_windows(weekday, minute, rule.windows)
