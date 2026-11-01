"""Effective limits and prices: plan values overlaid by ``orgs.limits`` overrides (specs/24 §2)."""

from __future__ import annotations

from typing import get_args

import pytest
from billing_helpers import make_plan

from va_billing.plans import (
    DEFAULT_PER_DESTINATION_DAILY,
    PricedMeter,
    effective_limits,
    effective_price,
    effective_pricing,
    int_override,
)


def test_build_plan_without_overrides() -> None:
    limits = effective_limits(make_plan("build"), {})
    assert (limits.daily_calls, limits.hourly_calls, limits.concurrency, limits.voice_clones) == (
        2000,
        1000,
        50,
        5,
    )
    assert limits.connected_rate_micros_per_min == 100_000
    assert limits.international_hourly_calls == 100
    assert limits.per_destination_daily_calls == DEFAULT_PER_DESTINATION_DAILY
    assert (limits.numbers, limits.sms_per_day, limits.international_enabled) == (None, None, None)


@pytest.mark.parametrize(
    ("plan_id", "cap"), [("start", 10), ("build", 100), ("scale", 300), ("enterprise", None)]
)
def test_international_hourly_caps_per_plan(plan_id: str, cap: int | None) -> None:
    assert effective_limits(make_plan(plan_id), {}).international_hourly_calls == cap


def test_enterprise_caps_are_unlimited() -> None:
    limits = effective_limits(make_plan("enterprise"), {})
    assert (limits.daily_calls, limits.hourly_calls, limits.concurrency) == (None, None, None)


def test_no_plan_gives_nulls_and_the_default_destination_cap() -> None:
    limits = effective_limits(None, {"concurrency": 3, "international_enabled": True})
    assert limits.concurrency == 3
    assert limits.international_enabled is True
    assert limits.daily_calls is None
    assert limits.per_destination_daily_calls == DEFAULT_PER_DESTINATION_DAILY


def test_overrides_win_and_bad_types_are_ignored() -> None:
    overrides: dict[str, object] = {
        "daily_calls": 7,
        "hourly_calls": True,
        "concurrency": "9",
        "international_hourly_calls": 42,
        "international_enabled": 1,
        "unknown": 5,
    }
    limits = effective_limits(make_plan("build"), overrides)
    assert limits.daily_calls == 7
    assert limits.hourly_calls == 1000
    assert limits.concurrency == 50
    assert limits.international_hourly_calls == 42
    assert limits.international_enabled is None


def test_per_destination_override_only_on_enterprise() -> None:
    overrides: dict[str, object] = {"per_destination_daily_calls": 20}
    assert effective_limits(make_plan("enterprise"), overrides).per_destination_daily_calls == 20
    assert effective_limits(make_plan("scale"), overrides).per_destination_daily_calls == 5
    bad: dict[str, object] = {"per_destination_daily_calls": False}
    assert effective_limits(make_plan("enterprise"), bad).per_destination_daily_calls == 5


def test_int_override() -> None:
    assert int_override(3) == 3
    assert int_override(True) is None
    assert int_override(3.0) is None
    assert int_override(None) is None


def test_pricing_override_beats_plan() -> None:
    overrides: dict[str, object] = {
        "connected_rate_micros_per_min": 70_000,
        "transfer_rate_micros_per_min": 20_000,
        "sms_rate_micros": 15_000,
        "widget_message_micros": 5_000,
        "min_attempt_micros": 1,
    }
    pricing = effective_pricing(make_plan("enterprise"), overrides)
    assert pricing.snapshot() == {
        "connected_rate_micros_per_min": 70_000,
        "transfer_rate_micros_per_min": 20_000,
        "min_attempt_micros": 15_000,
        "sms_rate_micros": 15_000,
        "widget_message_micros": 5_000,
        "tts_rate_micros_per_1k_chars": 120_000,
    }


def test_pricing_from_plan() -> None:
    pricing = effective_pricing(make_plan("start"), {"connected_rate_micros_per_min": "1"})
    assert pricing.connected_rate_micros_per_min == 120_000
    assert pricing.transfer_rate_micros_per_min == 50_000


@pytest.mark.parametrize(
    ("meter", "price"),
    [
        ("call_connected_seconds", 100_000),
        ("call_transfer_seconds", 40_000),
        ("call_attempt", 15_000),
        ("sms_segment_out", 20_000),
        ("sms_segment_in", 20_000),
        ("widget_message", 10_000),
    ],
)
def test_effective_price(meter: PricedMeter, price: int) -> None:
    assert effective_price(effective_pricing(make_plan("build"), {}), meter) == price


def test_every_priced_meter_has_a_rate() -> None:
    pricing = effective_pricing(make_plan("build"), {})
    assert all(effective_price(pricing, meter) > 0 for meter in get_args(PricedMeter))
