"""Effective org limits: plan values overlaid by ``orgs.limits`` overrides (specs/24 §2; specs/04 §8.1)."""

from __future__ import annotations

import pytest

from va_api.routers.org_schemas import effective_limits
from va_db.models import Plan


def build_plan() -> Plan:
    return Plan(
        id="build",
        name="Build",
        connected_rate_micros_per_min=100_000,
        transfer_rate_micros_per_min=40_000,
        sms_rate_micros=20_000,
        widget_message_micros=10_000,
        daily_call_cap=2000,
        hourly_call_cap=1000,
        concurrency_cap=50,
        voice_clone_cap=None,
    )


def test_plan_values_without_overrides() -> None:
    limits = effective_limits(build_plan(), {}).model_dump()
    assert limits == {
        "connected_rate_micros_per_min": 100_000,
        "transfer_rate_micros_per_min": 40_000,
        "sms_rate_micros": 20_000,
        "widget_message_micros": 10_000,
        "daily_calls": 2000,
        "hourly_calls": 1000,
        "concurrency": 50,
        "voice_clones": None,
        "numbers": None,
        "international_enabled": None,
        "sms_per_day": None,
    }


def test_no_plan_gives_only_overrides() -> None:
    limits = effective_limits(None, {"concurrency": 3, "international_enabled": True})
    assert limits.concurrency == 3
    assert limits.international_enabled is True
    assert limits.daily_calls is None
    assert limits.connected_rate_micros_per_min is None


def test_overrides_replace_plan_values() -> None:
    overrides = {
        "connected_rate_micros_per_min": 80_000,
        "daily_calls": 10_000,
        "voice_clones": 40,
        "numbers": 500,
        "sms_per_day": 2500,
        "international_enabled": False,
    }
    limits = effective_limits(build_plan(), overrides)
    assert limits.connected_rate_micros_per_min == 80_000
    assert limits.daily_calls == 10_000
    assert limits.voice_clones == 40
    assert limits.numbers == 500
    assert limits.sms_per_day == 2500
    assert limits.international_enabled is False
    assert limits.hourly_calls == 1000


@pytest.mark.parametrize(
    "overrides",
    [
        {"rate_per_sec": 99, "colour": "teal"},
        {"concurrency": True},
        {"concurrency": "7"},
        {"concurrency": 7.5},
        {"concurrency": None},
        {"international_enabled": 1},
        {"international_enabled": "yes"},
    ],
)
def test_unknown_and_wrongly_typed_overrides_are_ignored(overrides: dict[str, object]) -> None:
    assert effective_limits(build_plan(), overrides) == effective_limits(build_plan(), {})
