"""Value rules of `OrgSettings` (specs/04 §7, Decision "OrgSettings fields (P02-T12a)" item 4)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from va_domain.org import SETTINGS_GROUPS, GuardRailConfig, OrgSettings, field_path, load_settings

VOICE_ID = "0192f3c4-5a6b-7c8d-9e0f-1a2b3c4d5e6f"


def _window(start: str, end: str, *days: str) -> dict[str, Any]:
    return {"days": list(days) or ["mon"], "start": start, "end": end}


def _error_paths(stored: dict[str, Any]) -> list[str]:
    with pytest.raises(ValidationError) as caught:
        load_settings(stored)
    return [field_path(error["loc"]) for error in caught.value.errors()]


INVALID: list[tuple[dict[str, Any], str]] = [
    ({"timezone": "Mars/Olympus_Mons"}, "timezone"),
    ({"timezone": "America"}, "timezone"),
    ({"timezone": "/etc/localtime"}, "timezone"),
    ({"timezone": ""}, "timezone"),
    ({"default_language": "en_US"}, "default_language"),
    ({"default_language": "a" * 17}, "default_language"),
    ({"default_voice": ""}, "default_voice"),
    ({"default_voice": "v" * 101}, "default_voice"),
    ({"default_from_number": "4155550100"}, "default_from_number"),
    ({"default_from_number": "+04155550100"}, "default_from_number"),
    ({"default_max_duration_min": 0}, "default_max_duration_min"),
    ({"default_max_duration_min": 241}, "default_max_duration_min"),
    ({"default_webhook": "ftp://hooks.acme-dental.test/calls"}, "default_webhook"),
    ({"default_webhook": "/calls"}, "default_webhook"),
    ({"default_webhook": "https://hooks.acme-dental.test/" + "a" * 2048}, "default_webhook"),
    ({"default_webhook_events": ["call.ended", "call.ended"]}, "default_webhook_events"),
    ({"default_webhook_events": [""]}, "default_webhook_events[0]"),
    ({"default_webhook_events": [f"event.{index}" for index in range(51)]}, "default_webhook_events"),
    ({"default_voice_id": "karen"}, "default_voice_id"),
    ({"default_country": "us"}, "default_country"),
    ({"default_country": "USA"}, "default_country"),
    ({"call_defaults": ["record"]}, "call_defaults"),
    ({"dial_timeout_s": 4}, "dial_timeout_s"),
    ({"dial_timeout_s": 121}, "dial_timeout_s"),
    ({"transfer_caller_id_mode": "caller"}, "transfer_caller_id_mode"),
    ({"transfer_failed_message": ""}, "transfer_failed_message"),
    ({"max_duration_message": "m" * 501}, "max_duration_message"),
    ({"no_input_goodbye": ""}, "no_input_goodbye"),
    ({"voice_overrides": {VOICE_ID: {"speed": 1.5}}}, f"voice_overrides.{VOICE_ID}.speed"),
    ({"voice_overrides": {VOICE_ID: {"pitch": 1}}}, f"voice_overrides.{VOICE_ID}.pitch"),
    ({"voice_overrides": {"karen": {}}}, "voice_overrides.karen"),
    ({"default_guard_rails": [{"type": "tcpa:quiet_hours"}]}, "default_guard_rails[0].type"),
    ({"default_guard_rails": [{"config": {}}]}, "default_guard_rails[0].type"),
    ({"default_guard_rails": [{"type": "tcpa:opt_out"}] * 11}, "default_guard_rails"),
    ({"webhook_format": "json"}, "webhook_format"),
    ({"baa_signed_at": "2026-10-29T10:00:00"}, "baa_signed_at"),
    ({"onboarding_completed_at": "yesterday"}, "onboarding_completed_at"),
    ({"pii_redaction": "everything"}, "pii_redaction"),
    ({"pii_redaction_entities": ["PII"]}, "pii_redaction_entities[0]"),
    ({"pii_redaction_entities": ["pii", "pii"]}, "pii_redaction_entities"),
    ({"session_max_age_days": 0}, "session_max_age_days"),
    ({"session_max_age_days": 91}, "session_max_age_days"),
    ({"ip_allowlist": ["10.0.0.0/33"]}, "ip_allowlist[0]"),
    ({"ip_allowlist": ["office"]}, "ip_allowlist[0]"),
    ({"ip_allowlist": [f"10.0.{index}.0/24" for index in range(101)]}, "ip_allowlist"),
    ({"llm": {"base_url": "http://llm.acme-dental.test/v1"}}, "llm.base_url"),
    ({"llm": {"api_key_ref": "Acme Key"}}, "llm.api_key_ref"),
    ({"llm": {"dialogue_model": ""}}, "llm.dialogue_model"),
    ({"llm": {"fallback_model": "m" * 201}}, "llm.fallback_model"),
    ({"llm": {"temperature_default": 2.5}}, "llm.temperature_default"),
    ({"llm": {"context_budget_tokens": 999}}, "llm.context_budget_tokens"),
    ({"llm": {"context_budget_tokens": 32001}}, "llm.context_budget_tokens"),
    ({"allowed_countries": ["US", "US"]}, "allowed_countries"),
    ({"allowed_countries": ["us"]}, "allowed_countries[0]"),
    ({"blocked_prefixes": ["1900"]}, "blocked_prefixes[0]"),
    ({"blocked_prefixes": ["+1" + "9" * 15]}, "blocked_prefixes[0]"),
    ({"amd_default": "basic"}, "amd_default"),
    ({"background_track_default": "stadium"}, "background_track_default"),
    ({"post_call_eval_setup_id": "eval-1"}, "post_call_eval_setup_id"),
    ({"summary_prompt_default": "s" * 2001}, "summary_prompt_default"),
    ({"dispositions_default": [""]}, "dispositions_default[0]"),
    ({"dispositions_default": [f"d{index}" for index in range(51)]}, "dispositions_default"),
    ({"low_balance_threshold_micros": -1}, "low_balance_threshold_micros"),
    ({"notification_emails": ["billing"]}, "notification_emails[0]"),
    ({"notification_emails": ["ops@localhost"]}, "notification_emails[0]"),
    ({"cnam_display_name": "Acme-Dental"}, "cnam_display_name"),
    ({"cnam_display_name": "Acme Dental Group"}, "cnam_display_name"),
    ({"batch_concurrency_share": 0.05}, "batch_concurrency_share"),
    ({"batch_concurrency_share": 1.5}, "batch_concurrency_share"),
    ({"compliance": {"calling_hours": []}}, "compliance.calling_hours"),
    ({"compliance": {"calling_hours": [_window("08:00", "09:00")] * 15}}, "compliance.calling_hours"),
    (
        {"compliance": {"calling_hours": [_window("08:00", "12:00"), _window("11:00", "14:00")]}},
        "compliance.calling_hours",
    ),
    ({"compliance": {"calling_hours": [_window("24:00", "23:30")]}}, "compliance.calling_hours[0].start"),
    ({"compliance": {"calling_hours": [_window("21:00", "08:00")]}}, "compliance.calling_hours[0]"),
    ({"compliance": {"max_attempts_per_number_per_day": -1}}, "compliance.max_attempts_per_number_per_day"),
    ({"compliance": {"max_attempts_per_number_per_day": 101}}, "compliance.max_attempts_per_number_per_day"),
    ({"compliance": {"ai_disclosure_policy": "sometimes"}}, "compliance.ai_disclosure_policy"),
    ({"compliance": {"recording_disclosure_policy": "maybe"}}, "compliance.recording_disclosure_policy"),
    ({"mfa_required": None}, "mfa_required"),
    ({"compliance": None}, "compliance"),
    ({"analysis": {"retranscribe": "sometimes"}}, "analysis.retranscribe"),
]


@pytest.mark.parametrize(("stored", "path"), INVALID)
def test_invalid_value_is_rejected_at_its_path(stored: dict[str, Any], path: str) -> None:
    assert _error_paths(stored) == [path]


def test_unknown_top_level_key_is_rejected() -> None:
    assert _error_paths({"calling_hours": {"start": "08:00"}}) == ["calling_hours"]


@pytest.mark.parametrize("group", sorted(SETTINGS_GROUPS))
def test_unknown_group_key_is_rejected(group: str) -> None:
    assert _error_paths({group: {"surprise": True}}) == [f"{group}.surprise"]


def test_guard_rail_keeps_its_other_keys() -> None:
    rail = {"type": "custom", "name": "no_medical_advice", "check": "agent", "action": {"type": "end_call"}}
    settings = load_settings({"default_guard_rails": [rail]})
    assert settings.default_guard_rails[0].model_dump() == rail
    assert isinstance(settings.default_guard_rails[0], GuardRailConfig)


def test_ip_allowlist_is_normalized() -> None:
    settings = load_settings({"ip_allowlist": ["10.1.2.3/8", "203.0.113.7", "2001:db8::1/32"]})
    assert settings.ip_allowlist == ["10.0.0.0/8", "203.0.113.7/32", "2001:db8::/32"]


def test_notification_email_domain_is_lower_cased() -> None:
    settings = load_settings({"notification_emails": [" Billing@Acme-Dental.TEST "]})
    assert settings.notification_emails == ["Billing@acme-dental.test"]


def test_valid_values_are_accepted() -> None:
    stored: dict[str, Any] = {
        "timezone": "UTC",
        "default_language": "pt-BR",
        "default_from_number": "+14155550100",
        "default_webhook": "http://hooks.acme-dental.test/calls",
        "default_voice_id": VOICE_ID,
        "voice_overrides": {VOICE_ID: {"stability": 0.3}},
        "baa_signed_at": "2026-10-29T10:00:00Z",
        "blocked_prefixes": ["+1900", "+881"],
        "cnam_display_name": "Acme Dental",
        "llm": {
            "base_url": "https://llm.acme-dental.test/v1",
            "api_key_ref": "acme_llm-key",
            "temperature_default": 0,
        },
        "analysis": {"retranscribe": False},
        "call_defaults": {"no_input_timeout_ms": 6000, "record": True},
        "compliance": {
            "calling_hours": [
                _window("09:00", "12:00", "mon", "tue"),
                _window("12:00", "17:00", "mon"),
                _window("10:00", "14:00", "sat"),
            ]
        },
    }
    settings = load_settings(stored)
    assert [overrides.stability for overrides in settings.voice_overrides.values()] == [0.3]
    assert settings.analysis.retranscribe is False
    assert settings.call_defaults == {"no_input_timeout_ms": 6000, "record": True}


def test_field_path_handles_every_location_shape() -> None:
    assert field_path(()) == ""
    assert field_path(("timezone",)) == "timezone"
    assert field_path(("compliance", "calling_hours", 0, "end")) == "compliance.calling_hours[0].end"
    assert field_path(("voice_overrides", "karen", "[key]")) == "voice_overrides.karen"
    assert field_path(("default_guard_rails", 2)) == "default_guard_rails[2]"


def test_settings_json_schema_builds() -> None:
    schema = OrgSettings.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["GuardRailConfig"]["additionalProperties"] is True
