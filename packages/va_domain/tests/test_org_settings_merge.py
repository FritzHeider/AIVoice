"""Merge, sparse storage, and compliance-path helpers (specs/04 §7 Decision "OrgSettings fields", item 5)."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import pytest

from va_domain.org import (
    COMPLIANCE_PATHS,
    SETTINGS_GROUPS,
    OrgSettings,
    load_settings,
    merge_settings,
    needs_compliance,
    sent_paths,
    sparse_settings,
)

SPEC_04 = Path(__file__).resolve().parents[3] / "specs" / "04-auth-tenancy-rbac.md"
VOICE_A = "0192f3c4-5a6b-7c8d-9e0f-1a2b3c4d5e6f"
VOICE_B = "0192f3c4-5a6b-7c8d-9e0f-1a2b3c4d5e70"


def test_groups_merge_key_by_key() -> None:
    stored = {"llm": {"dialogue_model": "acme-large", "temperature_default": 0.4}}
    merged = merge_settings(stored, {"llm": {"temperature_default": 0.2}})
    assert merged == {"llm": {"dialogue_model": "acme-large", "temperature_default": 0.2}}


def test_a_group_absent_from_storage_is_taken_as_sent() -> None:
    assert merge_settings({}, {"engine": {"speculative": False}}) == {"engine": {"speculative": False}}


def test_lists_and_maps_are_replaced_whole() -> None:
    stored: dict[str, Any] = {
        "allowed_countries": ["US", "CA", "MX"],
        "voice_overrides": {VOICE_A: {"speed": 1.1}},
        "compliance": {"calling_hours": [{"days": ["mon"], "start": "09:00", "end": "17:00"}]},
    }
    patch: dict[str, Any] = {
        "allowed_countries": ["US"],
        "voice_overrides": {VOICE_B: {"stability": 0.2}},
        "compliance": {"calling_hours": [{"days": ["sat"], "start": "10:00", "end": "14:00"}]},
    }
    assert merge_settings(stored, patch) == patch


def test_null_sets_a_nullable_field_to_null() -> None:
    merged = merge_settings(
        {"default_webhook": "https://hooks.acme-dental.test/calls"}, {"default_webhook": None}
    )
    assert merged == {"default_webhook": None}
    assert load_settings(merged).default_webhook is None


def test_inputs_are_not_mutated_and_the_result_shares_nothing() -> None:
    stored: dict[str, Any] = {"llm": {"dialogue_model": "acme-large"}, "dispositions_default": ["booked"]}
    patch: dict[str, Any] = {
        "llm": {"router_model": "acme-small"},
        "notification_emails": ["ops@acme-dental.test"],
    }
    stored_copy, patch_copy = copy.deepcopy(stored), copy.deepcopy(patch)
    merged = merge_settings(stored, patch)
    merged["llm"]["dialogue_model"] = "changed"
    merged["dispositions_default"].append("changed")
    merged["notification_emails"].append("changed")
    assert (stored, patch) == (stored_copy, patch_copy)


def test_sent_paths_name_group_keys_with_dots() -> None:
    patch = {
        "timezone": "UTC",
        "compliance": {"calling_hours": [], "state_quiet_hours": False},
        "privacy": {},
    }
    assert sent_paths(patch) == ["timezone", "compliance.calling_hours", "compliance.state_quiet_hours"]


@pytest.mark.parametrize(
    "patch",
    [
        {"mfa_required": True},
        {"ip_allowlist": []},
        {"baa_signed_at": None},
        {"compliance": {"enforce_calling_hours": True}},
        {"privacy": {"zero_retention": True}},
        {"redaction": {"enabled": True}},
        {"timezone": "UTC", "blocked_prefixes": ["+1900"]},
    ],
)
def test_compliance_settings_need_the_compliance_permission(patch: dict[str, Any]) -> None:
    assert needs_compliance(patch)


@pytest.mark.parametrize(
    "patch",
    [{}, {"timezone": "UTC"}, {"llm": {"dialogue_model": "acme-large"}}, {"compliance": {}}, {"sms": {}}],
)
def test_other_settings_do_not(patch: dict[str, Any]) -> None:
    assert not needs_compliance(patch)


def test_compliance_paths_match_the_decision() -> None:
    text = SPEC_04.read_text(encoding="utf-8")
    start = text.index("Compliance settings (writing any of them needs `org.compliance`)")
    listed = re.findall(r"`([a-z_]+)`", text[start : text.index("Cross-field rules", start)])
    assert set(listed) - {"org"} == COMPLIANCE_PATHS
    assert set(OrgSettings.model_fields) >= COMPLIANCE_PATHS


def test_every_group_is_a_settings_field() -> None:
    assert set(OrgSettings.model_fields) >= SETTINGS_GROUPS


def test_defaults_round_trip_through_storage() -> None:
    full = OrgSettings().model_dump(mode="json")
    assert load_settings(full) == OrgSettings()
    assert sparse_settings(load_settings(full)) == full


def test_sparse_storage_keeps_only_set_keys() -> None:
    settings = load_settings({"mfa_required": True})
    assert settings.mfa_required is True
    assert settings.timezone == "America/Los_Angeles"
    assert settings.compliance.calling_hours[0].days == ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    assert sparse_settings(settings) == {"mfa_required": True}


def test_sparse_storage_is_recursive_and_normalized() -> None:
    settings = load_settings(
        {
            "ip_allowlist": ["10.1.2.3/8"],
            "compliance": {"state_quiet_hours": False},
            "default_guard_rails": [{"type": "tcpa:opt_out", "action": {"type": "end_call"}}],
        }
    )
    assert sparse_settings(settings) == {
        "ip_allowlist": ["10.0.0.0/8"],
        "compliance": {"state_quiet_hours": False},
        "default_guard_rails": [{"type": "tcpa:opt_out", "action": {"type": "end_call"}}],
    }


def test_a_patch_body_parsed_with_the_model_yields_what_was_sent() -> None:
    body = {"llm": {"temperature_default": 0.3}, "timezone": "Europe/Berlin"}
    sent = OrgSettings.model_validate(body).model_dump(mode="json", exclude_unset=True)
    assert sent == body
    assert sent_paths(sent) == ["timezone", "llm.temperature_default"]
