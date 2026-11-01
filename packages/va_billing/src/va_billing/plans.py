"""Effective org limits and prices: the plan row overlaid by the ``orgs.limits`` overrides (specs/24 §2).

An override replaces the plan value; unknown keys and wrongly typed values are ignored (a boolean is never an
integer override). ``per_destination_daily_calls`` is honoured only on the enterprise plan (specs/28 §9.3
"enterprise configurable"); ``international_hourly_calls`` overrides the per-plan international hourly cap of
specs/24 §6 item 7 (Decision P05-T04b under specs/24 §6).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Final, Literal

from va_db.models import Plan

__all__ = [
    "BOOL_OVERRIDE_KEYS",
    "DEFAULT_PER_DESTINATION_DAILY",
    "ENTERPRISE_PLAN",
    "INTERNATIONAL_HOURLY_CAPS",
    "INT_OVERRIDE_KEYS",
    "EffectiveLimits",
    "EffectivePricing",
    "PricedMeter",
    "effective_limits",
    "effective_price",
    "effective_pricing",
    "int_override",
]

INT_OVERRIDE_KEYS: Final = (
    "connected_rate_micros_per_min",
    "transfer_rate_micros_per_min",
    "sms_rate_micros",
    "widget_message_micros",
    "daily_calls",
    "hourly_calls",
    "concurrency",
    "voice_clones",
    "numbers",
    "sms_per_day",
    "international_hourly_calls",
    "per_destination_daily_calls",
)
BOOL_OVERRIDE_KEYS: Final = ("international_enabled",)
ENTERPRISE_PLAN: Final = "enterprise"
INTERNATIONAL_HOURLY_CAPS: Final[Mapping[str, int]] = {"start": 10, "build": 100, "scale": 300}
DEFAULT_PER_DESTINATION_DAILY: Final = 5

PricedMeter = Literal[
    "call_connected_seconds",
    "call_transfer_seconds",
    "call_attempt",
    "sms_segment_out",
    "sms_segment_in",
    "widget_message",
]


def int_override(value: object) -> int | None:
    """``value`` when it is an integer (booleans excluded), else ``None``."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@dataclass(frozen=True, slots=True)
class EffectiveLimits:
    """Rates and caps after overrides; ``None`` caps are unlimited (``numbers``, ``sms_per_day``: not set)."""

    connected_rate_micros_per_min: int | None
    transfer_rate_micros_per_min: int | None
    sms_rate_micros: int | None
    widget_message_micros: int | None
    daily_calls: int | None
    hourly_calls: int | None
    concurrency: int | None
    voice_clones: int | None
    numbers: int | None
    international_enabled: bool | None
    sms_per_day: int | None
    international_hourly_calls: int | None
    per_destination_daily_calls: int


def _plan_values(plan: Plan | None) -> dict[str, int | None]:
    values: dict[str, int | None] = dict.fromkeys(INT_OVERRIDE_KEYS)
    if plan is None:
        return values
    return values | {
        "connected_rate_micros_per_min": plan.connected_rate_micros_per_min,
        "transfer_rate_micros_per_min": plan.transfer_rate_micros_per_min,
        "sms_rate_micros": plan.sms_rate_micros,
        "widget_message_micros": plan.widget_message_micros,
        "daily_calls": plan.daily_call_cap,
        "hourly_calls": plan.hourly_call_cap,
        "concurrency": plan.concurrency_cap,
        "voice_clones": plan.voice_clone_cap,
        "international_hourly_calls": INTERNATIONAL_HOURLY_CAPS.get(plan.id),
    }


def effective_limits(plan: Plan | None, overrides: Mapping[str, object]) -> EffectiveLimits:
    """The plan's values (all ``None`` without a plan) with every well-typed override applied."""
    values = _plan_values(plan)
    for key in INT_OVERRIDE_KEYS:
        number = int_override(overrides.get(key))
        if number is not None:
            values[key] = number
    flag = overrides.get("international_enabled")
    per_destination = values["per_destination_daily_calls"]
    enterprise = plan is not None and plan.id == ENTERPRISE_PLAN
    return EffectiveLimits(
        connected_rate_micros_per_min=values["connected_rate_micros_per_min"],
        transfer_rate_micros_per_min=values["transfer_rate_micros_per_min"],
        sms_rate_micros=values["sms_rate_micros"],
        widget_message_micros=values["widget_message_micros"],
        daily_calls=values["daily_calls"],
        hourly_calls=values["hourly_calls"],
        concurrency=values["concurrency"],
        voice_clones=values["voice_clones"],
        numbers=values["numbers"],
        international_enabled=flag if isinstance(flag, bool) else None,
        sms_per_day=values["sms_per_day"],
        international_hourly_calls=values["international_hourly_calls"],
        per_destination_daily_calls=(
            per_destination if enterprise and per_destination is not None else DEFAULT_PER_DESTINATION_DAILY
        ),
    )


@dataclass(frozen=True, slots=True)
class EffectivePricing:
    """The rates a call is billed at, snapshotted into ``calls.config.pricing`` at dispatch (specs/24 §4)."""

    connected_rate_micros_per_min: int
    transfer_rate_micros_per_min: int
    min_attempt_micros: int
    sms_rate_micros: int
    widget_message_micros: int
    tts_rate_micros_per_1k_chars: int

    def snapshot(self) -> dict[str, int]:
        """The rates as a JSON object (field name → micro-dollars)."""
        return asdict(self)


def _rate(overrides: Mapping[str, object], key: str, plan_value: int) -> int:
    number = int_override(overrides.get(key))
    return plan_value if number is None else number


def effective_pricing(plan: Plan, overrides: Mapping[str, object]) -> EffectivePricing:
    """The plan's prices with the four rate overrides of specs/24 §2 applied (min attempt and TTS: plan)."""
    return EffectivePricing(
        connected_rate_micros_per_min=_rate(
            overrides, "connected_rate_micros_per_min", plan.connected_rate_micros_per_min
        ),
        transfer_rate_micros_per_min=_rate(
            overrides, "transfer_rate_micros_per_min", plan.transfer_rate_micros_per_min
        ),
        min_attempt_micros=plan.min_attempt_micros,
        sms_rate_micros=_rate(overrides, "sms_rate_micros", plan.sms_rate_micros),
        widget_message_micros=_rate(overrides, "widget_message_micros", plan.widget_message_micros),
        tts_rate_micros_per_1k_chars=plan.tts_rate_micros_per_1k_chars,
    )


_METER_RATES: Final[Mapping[PricedMeter, str]] = {
    "call_connected_seconds": "connected_rate_micros_per_min",
    "call_transfer_seconds": "transfer_rate_micros_per_min",
    "call_attempt": "min_attempt_micros",
    "sms_segment_out": "sms_rate_micros",
    "sms_segment_in": "sms_rate_micros",
    "widget_message": "widget_message_micros",
}


def effective_price(pricing: EffectivePricing, meter: PricedMeter) -> int:
    """The org's price of ``meter`` in micro-dollars (per minute for the two call-seconds meters)."""
    rate: int = getattr(pricing, _METER_RATES[meter])
    return rate
