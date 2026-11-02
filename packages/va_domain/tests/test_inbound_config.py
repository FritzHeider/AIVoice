"""`InboundConfig` (specs/16 §4, Decision P05-T12a): every field accepts a valid value and rejects an invalid
one, the runner rule, outbound-only keys, `load_inbound_config` errors, and `maintenance_active`."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from va_domain.numbers import (
    OUTBOUND_ONLY_FIELDS,
    ROUTING_FIELDS,
    InboundConfig,
    InboundConfigError,
    Maintenance,
    load_inbound_config,
    maintenance_active,
)

UUID_TEXT = "0190f1d2-7a3b-7c4d-8e5f-0123456789ab"
TOOL = {
    "name": "lookup",
    "description": "Look up an order.",
    "url": "https://api.shop.test/orders",
    "method": "GET",
}

# field -> (valid value, invalid value)
CASES: dict[str, tuple[Any, Any]] = {
    "prompt": ("Answer for Bella Pasta.", "x" * 16_001),
    "pathway_id": (UUID_TEXT, "not-a-uuid"),
    "pathway_version": ("staging", "draft"),
    "persona_id": (UUID_TEXT, 7),
    "persona_version": ("draft", "staging"),
    "agent_id": (UUID_TEXT, "agent"),
    "agent_version": ("1.2.3", "v1"),
    "voice": ("Karen", ""),
    "first_sentence": ("Hello!", "x" * 1001),
    "wait_for_greeting": (True, "maybe"),
    "background_track": ("office", "stadium"),
    "noise_cancellation": (False, "sometimes"),
    "block_interruptions": (True, "nope"),
    "interruption_threshold": (300, 10),
    "interruptibility": (4, 5),
    "model": ("turbo", ""),
    "temperature": (0.2, 1.5),
    "language": ("en-US", "klingon"),
    "timezone": ("Europe/Berlin", "Mars/Olympus"),
    "request_data": ({"shop": "Bella"}, ["not", "an", "object"]),
    "metadata": ({"crm": "42"}, "text"),
    "tools": ([f"TL-{UUID_TEXT}", TOOL], ["not-a-tool"]),
    "dynamic_data": ([], [{"url": 5}]),
    "transfer_phone_number": ("+14155550199", "call me maybe"),
    "transfer_list": ({"default": "+14155550199"}, {"default": "nowhere"}),
    "warm_transfer": (None, {"unknown": 1}),
    "fallback_number": ("+14155550199", "4155550199"),
    "maintenance": ({"enabled": True, "message": "Closed today."}, {"enabled": True, "windows": []}),
    "memory_id": (UUID_TEXT, "memory"),
    "memory_enabled": (True, "on and off"),
    "pronunciation_guide": ([{"word": "VA", "pronunciation": "vee ay"}], [{"word": "VA"}]),
    "keywords": (["Bella:5"], ["Bella:99"]),
    "summary_prompt": ("Summarize.", "x" * 2001),
    "analysis_schema": ({"order_id": "string"}, "schema"),
    "dispositions": (["BOOKED"], ["booked"]),
    "citation_schema_ids": ([UUID_TEXT], ["citation"]),
    "post_call_evals": (None, {"unknown": True}),
    "webhook": ("https://hooks.shop.test/calls", "ftp://hooks.shop.test"),
    "webhook_events": (["call", "webhook"], ["call.exploded"]),
    "record": (True, "always"),
    "max_duration": (240, 241),
    "guard_rails": ([], [{"type": "unknown"}]),
    "voicemail": ({"action": "hangup"}, {"action": "explode"}),
    "ignore_button_press": (True, "never"),
    "hd_audio": (True, "hi-fi"),
    "allowed_callers": (["+14155550123"], ["+1415555"]),
    "blocked_callers": (["+442071838750"], ["+44 20 7183 8750"]),
    "block_dnc_callers": (True, "sure"),
    "byo_original_connection_id": ("1684641123236054244", ""),
}


@pytest.mark.parametrize("field", sorted(CASES))
def test_every_field_accepts_a_valid_value(field: str) -> None:
    valid, _ = CASES[field]
    config = load_inbound_config({field: valid})
    assert field in config.model_fields_set


@pytest.mark.parametrize("field", sorted(CASES))
def test_every_field_rejects_an_invalid_value(field: str) -> None:
    _, invalid = CASES[field]
    with pytest.raises(InboundConfigError) as caught:
        load_inbound_config({field: invalid})
    assert caught.value.field is not None
    assert caught.value.field.split(".")[0].split("[")[0] == field


def test_unknown_keys_are_rejected() -> None:
    with pytest.raises(InboundConfigError) as caught:
        load_inbound_config({"promt": "typo"})
    assert caught.value.field == "promt"


@pytest.mark.parametrize("name", sorted(OUTBOUND_ONLY_FIELDS - {"from_"}))
def test_outbound_only_parameters_are_rejected(name: str) -> None:
    with pytest.raises(InboundConfigError, match=f"{name} cannot be set on an inbound number"):
        load_inbound_config({name: None})


def test_routing_fields_are_inbound_config_fields() -> None:
    assert set(InboundConfig.model_fields).issuperset(ROUTING_FIELDS)


def test_max_duration_lower_bound_is_one_minute() -> None:
    with pytest.raises(InboundConfigError) as caught:
        load_inbound_config({"max_duration": 0.5})
    assert caught.value.field == "max_duration"
    assert load_inbound_config({"max_duration": 1}).max_duration == 1


def test_caller_lists_are_capped() -> None:
    numbers = [f"+1415555{index:04d}" for index in range(1001)]
    with pytest.raises(InboundConfigError) as caught:
        load_inbound_config({"allowed_callers": numbers})
    assert caught.value.field == "allowed_callers"


@pytest.mark.parametrize(
    "body",
    [
        {"prompt": "Hi", "pathway_id": UUID_TEXT},
        {"prompt": "Hi", "agent_id": UUID_TEXT},
        {"pathway_id": UUID_TEXT, "agent_id": UUID_TEXT},
    ],
)
def test_at_most_one_runner(body: dict[str, Any]) -> None:
    with pytest.raises(InboundConfigError, match="At most one of prompt, pathway_id, agent_id") as caught:
        load_inbound_config(body)
    assert caught.value.field is None


@pytest.mark.parametrize("other", [{"prompt": "Hi"}, {"agent_id": UUID_TEXT}])
def test_persona_combines_only_with_a_pathway(other: dict[str, Any]) -> None:
    with pytest.raises(InboundConfigError, match="persona_id may only be combined with pathway_id"):
        load_inbound_config({"persona_id": UUID_TEXT, **other})


def test_persona_with_a_pathway_is_allowed() -> None:
    config = load_inbound_config({"persona_id": UUID_TEXT, "pathway_id": UUID_TEXT, "pathway_version": 3})
    assert (config.persona_id, config.pathway_id, config.pathway_version) == (
        uuid.UUID(UUID_TEXT),
        uuid.UUID(UUID_TEXT),
        3,
    )


def test_empty_config_defaults() -> None:
    config = load_inbound_config({})
    assert config.model_fields_set == set()
    assert (config.prompt, config.pathway_id, config.agent_id, config.persona_id) == (None, None, None, None)
    assert config.maintenance == Maintenance()
    assert (config.allowed_callers, config.blocked_callers, config.block_dnc_callers) == ([], [], False)
    assert config.fallback_number is None
    assert (config.interruptibility, config.temperature, config.max_duration) == (2, 0.7, 30)


def test_nested_errors_carry_the_field_path() -> None:
    with pytest.raises(InboundConfigError) as caught:
        load_inbound_config({"allowed_callers": ["+14155550123", "nope"]})
    assert caught.value.field == "allowed_callers[1]"
    assert "E.164" in caught.value.message


def test_maintenance_window_must_be_ordered() -> None:
    start = datetime(2026, 11, 2, 9, tzinfo=UTC)
    with pytest.raises(InboundConfigError, match="starts_at must not be after ends_at") as caught:
        load_inbound_config(
            {
                "maintenance": {
                    "enabled": True,
                    "starts_at": start.isoformat(),
                    "ends_at": "2026-11-01T09:00:00Z",
                }
            }
        )
    assert caught.value.field == "maintenance"


def test_maintenance_bounds_must_be_aware() -> None:
    with pytest.raises(ValidationError):
        Maintenance.model_validate({"enabled": True, "starts_at": "2026-11-02T09:00:00"})


START = datetime(2026, 11, 2, 9, tzinfo=UTC)
END = START + timedelta(hours=2)


@pytest.mark.parametrize(
    ("maintenance", "now", "active"),
    [
        (None, START, False),
        (Maintenance(enabled=False, starts_at=START, ends_at=END), START, False),
        (Maintenance(enabled=True), START, True),
        (Maintenance(enabled=True, starts_at=START), START - timedelta(seconds=1), False),
        (Maintenance(enabled=True, starts_at=START), START + timedelta(days=30), True),
        (Maintenance(enabled=True, ends_at=END), END + timedelta(seconds=1), False),
        (Maintenance(enabled=True, ends_at=END), END - timedelta(days=30), True),
        (Maintenance(enabled=True, starts_at=START, ends_at=END), START, True),
        (Maintenance(enabled=True, starts_at=START, ends_at=END), END, True),
        (Maintenance(enabled=True, starts_at=START, ends_at=END), START + timedelta(hours=1), True),
        (Maintenance(enabled=True, starts_at=START, ends_at=END), START - timedelta(microseconds=1), False),
        (Maintenance(enabled=True, starts_at=START, ends_at=END), END + timedelta(microseconds=1), False),
        (
            Maintenance(enabled=True, starts_at=START, ends_at=END),
            START.astimezone(timezone(timedelta(hours=-8))),
            True,
        ),
    ],
)
def test_maintenance_active(maintenance: Maintenance | None, now: datetime, active: bool) -> None:
    assert maintenance_active(maintenance, now) is active


def test_maintenance_active_needs_an_aware_now() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        maintenance_active(Maintenance(enabled=True), datetime(2026, 11, 2, 9))  # noqa: DTZ001 - the naive input


def test_a_non_object_config_is_rejected() -> None:
    with pytest.raises(ValidationError):
        InboundConfig.model_validate(["prompt"])
