"""`resolve_call_config` precedence table (specs/05-calls-api.md §4.3, §4.4, §3; Decision P05-T03b).

Every case starts from `{"phone_number", "task": "Hi"}` plus `body`, resolves it for a sparse org settings
object and a runner, and compares a subset of the dumped `SendCallConfig`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from resolution_helpers import (
    AGENT_ID,
    AGENT_VERSION_ID,
    AI_RAIL,
    ARM_ID,
    BASE_BODY,
    BUILTINS,
    DIALOGUE,
    EXPERIMENT_ID,
    PATHWAY_ID,
    PERSONA_ID,
    RECORDING_RAIL,
    ROUTER,
    VOICE_ID,
    dumped,
    pathway_ref,
    rail,
    resolve,
)

from va_domain.call_resolution import (
    AgentRunner,
    PathwayRunner,
    PersonaRunner,
    RunnerInputs,
)
from va_domain.calls import DEFAULT_ERROR_PHRASE

ID_A = "0192f5a1-7c3e-7d4a-9b1c-2f3e4d5c6d0a"
ID_B = "0192f5a1-7c3e-7d4a-9b1c-2f3e4d5c6d0b"
NO_TASK = {"task": None}
A, B, C = "+14155550101", "+14155550102", "+14155550103"


def _inline(name: str, url: str) -> dict[str, Any]:
    return {"name": name, "description": "Looks things up.", "url": url}


def _persona(config: dict[str, Any], *, legacy: bool = False, pathway: bool = False) -> PersonaRunner:
    return PersonaRunner(
        PERSONA_ID, None if legacy else 2, legacy, config, pathway_ref() if pathway else None
    )


def _agent(settings: dict[str, Any]) -> AgentRunner:
    return AgentRunner(AGENT_ID, AGENT_VERSION_ID, settings, None, EXPERIMENT_ID, ARM_ID)


PATHWAY = PathwayRunner(pathway_ref(call_defaults={"max_tokens": 400}, error_phrase="Hold on."))
PERSONA_BODY = {"task": None, "persona_id": str(PERSONA_ID)}
AGENT_BODY = {"task": None, "agent_id": str(AGENT_ID)}
PATHWAY_BODY = {"task": None, "pathway_id": str(PATHWAY_ID)}


@dataclass(frozen=True)
class Case:
    id: str
    expected: dict[str, Any]
    body: dict[str, Any] = field(default_factory=dict)
    org: dict[str, Any] = field(default_factory=dict)
    runner: RunnerInputs | None = None
    refs: dict[str, Any] = field(default_factory=dict)


CASES = [
    # platform defaults
    Case(
        "platform_defaults",
        {
            "runner_type": "task",
            "task": "Hi",
            "record": False,
            "max_duration_min": 30.0,
            "language": "babel-en",
            "timezone": "America/Los_Angeles",
            "temperature": 0.7,
            "background_track": None,
            "answered_by_enabled": True,
            "dispositions": BUILTINS,
            "guard_rails": [AI_RAIL],
            "max_reprompts": 2,
            "no_input_timeout_ms": 8000,
            "greeting_timeout_ms": 6000,
            "error_phrase": DEFAULT_ERROR_PHRASE,
        },
    ),
    # org default_* settings (only explicitly set ones are layered)
    Case("org_default_language", {"language": "es"}, org={"default_language": "es"}),
    Case("org_default_record", {"record": True}, org={"default_record": True}),
    Case("org_default_max_duration", {"max_duration_min": 12.0}, org={"default_max_duration_min": 12}),
    Case(
        "org_default_webhook",
        {"webhook": "https://hooks.acme.test/c", "webhook_events": ["call"]},
        org={"default_webhook": "https://hooks.acme.test/c", "default_webhook_events": ["call"]},
    ),
    Case("org_timezone", {"timezone": "America/New_York"}, org={"timezone": "America/New_York"}),
    Case("org_default_country", {"country": "CA"}, org={"default_country": "CA"}),
    Case("org_summary_prompt", {"summary_prompt": "Be brief."}, org={"summary_prompt_default": "Be brief."}),
    Case(
        "org_dispositions_default",
        {"dispositions": ["Hot lead", *BUILTINS]},
        org={"dispositions_default": ["Hot lead"]},
    ),
    Case("org_noise_cancellation", {"noise_cancellation": False}, org={"noise_cancellation_default": False}),
    Case(
        "org_background_phone_static",
        {"background_track": None},
        org={"background_track_default": "phone_static"},
    ),
    Case("org_background_none", {"background_track": "none"}, org={"background_track_default": "none"}),
    Case("org_background_cafe", {"background_track": "cafe"}, org={"background_track_default": "cafe"}),
    Case("org_amd_off", {"answered_by_enabled": False}, org={"amd_default": "off"}),
    Case("org_amd_premium", {"answered_by_enabled": True}, org={"amd_default": "premium"}),
    Case("org_memory_default", {"memory_enabled": True}, org={"memory": {"default_enabled": True}}),
    Case("org_temperature_default", {"temperature": 1.5}, org={"llm": {"temperature_default": 1.5}}),
    Case(
        "org_eval_setup_forces_record",
        {
            "post_call_evals": {"workbench_setup_id": ID_A, "workbench_setup_version_id": None},
            "record": True,
            "guard_rails": [AI_RAIL, RECORDING_RAIL],
        },
        org={"post_call_eval_setup_id": ID_A},
    ),
    Case(
        "call_defaults_over_default_settings",
        {"record": False},
        org={"default_record": True, "call_defaults": {"record": False}},
    ),
    Case(
        "call_defaults_derived_fields",
        {"no_input_timeout_ms": 6000, "max_reprompts": 4, "greeting_timeout_ms": 9000},
        org={"call_defaults": {"no_input_timeout_ms": 6000, "max_reprompts": 4, "greeting_timeout_ms": 9000}},
    ),
    Case("request_over_org", {"record": False}, body={"record": False}, org={"default_record": True}),
    # pathway runner
    Case(
        "pathway_defaults_over_org",
        {
            "runner_type": "pathway",
            "task": None,
            "max_tokens": 400,
            "pathway_id": str(PATHWAY_ID),
            "pathway_version": 3,
            "start_node_id": "greet",
            "error_phrase": "Hold on.",
        },
        body=PATHWAY_BODY,
        org={"call_defaults": {"max_tokens": 300}},
        runner=PATHWAY,
    ),
    Case(
        "request_over_pathway",
        {"max_tokens": 500, "start_node_id": "verify"},
        body={**PATHWAY_BODY, "max_tokens": 500, "start_node_id": "verify"},
        runner=PATHWAY,
    ),
    Case(
        "lower_request_data_kept",
        {"request_data": {"tier": "gold"}},
        body=PATHWAY_BODY,
        runner=PathwayRunner(pathway_ref(call_defaults={"request_data": {"tier": "gold"}})),
    ),
    Case(
        "request_data_replaced_by_empty_object",
        {"request_data": {}},
        body={**PATHWAY_BODY, "request_data": {}},
        runner=PathwayRunner(pathway_ref(call_defaults={"request_data": {"tier": "gold"}})),
    ),
    # persona runner
    Case(
        "versioned_persona_over_request",
        {"runner_type": "persona", "first_sentence": "Persona hi", "persona_version": 2},
        body={**PERSONA_BODY, "first_sentence": "Body hi"},
        runner=_persona({"first_sentence": "Persona hi", "prompt": "Persona prompt"}),
    ),
    Case(
        "versioned_persona_request_fills_unset",
        {"record": True, "max_tokens": 99, "task": "Persona prompt", "memory_enabled": True},
        body={**PERSONA_BODY, "max_tokens": 99},
        runner=_persona(
            {"prompt": "Persona prompt", "call_settings": {"record": True}, "memory": {"enabled": True}}
        ),
    ),
    Case(
        "legacy_persona_under_request",
        {"first_sentence": "Body hi", "persona_version": None},
        body={**PERSONA_BODY, "first_sentence": "Body hi"},
        runner=_persona({"first_sentence": "Persona hi", "prompt": "P"}, legacy=True),
    ),
    Case(
        "persona_settings_over_persona",
        {"first_sentence": "Override", "max_duration_min": 12.0},
        body={**PERSONA_BODY, "persona_settings": {"first_sentence": "Override", "max_duration": 12}},
        runner=_persona({"first_sentence": "Persona hi", "prompt": "P"}),
    ),
    Case(
        "persona_metadata_replaced_by_request",
        {"metadata": {}},
        body={**PERSONA_BODY, "metadata": {}},
        runner=_persona({"metadata": {"source": "persona"}, "prompt": "P"}),
    ),
    Case(
        "persona_metadata_used_when_absent",
        {"metadata": {"source": "persona"}},
        body=PERSONA_BODY,
        runner=_persona({"metadata": {"source": "persona"}, "prompt": "P"}),
    ),
    Case(
        "request_task_overrides_persona_prompt",
        {"task": "Body task"},
        body={**PERSONA_BODY, "task": "Body task"},
        runner=_persona({"prompt": "Persona prompt"}),
    ),
    Case(
        "persona_pathway_ignores_task",
        {"task": None, "pathway_id": str(PATHWAY_ID), "runner_type": "persona"},
        body={**PERSONA_BODY, "task": "Body task"},
        runner=_persona({"runner": "pathway"}, pathway=True),
    ),
    # agent runner
    Case(
        "agent_call_settings_under_request",
        {
            "runner_type": "agent",
            "task": "Agent prompt",
            "max_tokens": 222,
            "record": True,
            "agent_id": str(AGENT_ID),
            "agent_version_id": str(AGENT_VERSION_ID),
            "experiment_id": str(EXPERIMENT_ID),
            "experiment_arm_id": str(ARM_ID),
        },
        body={**AGENT_BODY, "max_tokens": 222},
        runner=_agent({"task": "Agent prompt", "max_tokens": 111, "record": True}),
    ),
    Case(
        "agent_request_data_replaced",
        {"request_data": {}},
        body={**AGENT_BODY, "request_data": {}},
        runner=_agent({"task": "P", "request_data": {"a": 1}}),
    ),
    # objects merged per key
    Case(
        "voice_settings_three_levels",
        {
            "voice_settings": {
                "stability": 0.6,
                "similarity_boost": 0.75,
                "style": 0.2,
                "use_speaker_boost": True,
                "speed": 1.1,
            }
        },
        body={"voice_settings": {"speed": 1.1}},
        org={"voice_overrides": {str(VOICE_ID): {"stability": 0.6, "style": 0.2}}},
    ),
    Case(
        "voice_settings_layers_merge_per_key",
        {"voice_settings": {"stability": 0.2, "speed": 1.1}},
        body={"voice_settings": {"speed": 1.1}},
        org={"call_defaults": {"voice_settings": {"stability": 0.2, "speed": 0.9}}},
    ),
    Case(
        "voicemail_merges_per_key",
        {"voicemail": {"action": "hangup", "message": "Org msg", "sensitive": True}},
        body={"voicemail": {"action": "hangup"}},
        org={"call_defaults": {"voicemail": {"message": "Org msg", "sensitive": True}}},
    ),
    Case(
        "voicemail_message_implies_leave_message",
        {"voicemail": {"action": "leave_message", "message": "Call us back.", "detect_timeout_ms": 9000}},
        body={"voicemail": {"message": "Call us back."}, "voicemail_detect_timeout_ms": 9000},
    ),
    Case(
        "ivr_mode_forces_voicemail_ignore",
        {"ivr_mode": True, "voicemail": {"action": "ignore", "sensitive": False}},
        body={"amd": True, "voicemail": {"action": "hangup", "sensitive": True}},
    ),
    Case(
        "warm_transfer_merges_per_key",
        {
            "warm_transfer": {
                "enabled": True,
                "timeout_s": 60,
                "hold_audio": "silence",
                "briefing_prompt": None,
            }
        },
        body={"warm_transfer": {"hold_audio": "silence"}},
        org={"call_defaults": {"warm_transfer": {"enabled": True, "timeout_s": 60}}},
    ),
    Case(
        "retry_merges_per_key",
        {"retry": {"wait": 600, "max_attempts": 3, "voicemail_action": "hangup"}},
        body={"retry": {"wait": 600}},
        org={"call_defaults": {"retry": {"wait": 120, "max_attempts": 3}}},
    ),
    Case(
        "transfer_list_merges_per_entry",
        {"transfer_list": {"default": C, "sales": B}},
        body={"transfer_list": {"default": C}},
        org={"call_defaults": {"transfer_list": {"default": A, "sales": B}}},
    ),
    Case(
        "analysis_schema_merges_per_key",
        {"analysis_schema": {"score": "number", "notes": "string"}},
        body={"analysis_schema": {"notes": "string"}},
        org={"call_defaults": {"analysis_schema": {"score": "number", "notes": "boolean"}}},
    ),
    Case(
        "dialing_strategy_replaced_whole",
        {"dialing_strategy": {"type": "local", "pool_id": None}},
        body={"local_dialing": True},
        org={"call_defaults": {"dialing_strategy": {"type": "custom_pooling", "pool_id": ID_A}}},
    ),
    Case(
        "post_call_evals_replaced_whole",
        {"post_call_evals": {"workbench_setup_id": None, "workbench_setup_version_id": ID_B}},
        body={"post_call_evals": {"workbench_setup_version_id": ID_B}},
        org={"post_call_eval_setup_id": ID_A},
    ),
    # tools
    Case(
        "tools_union_deduplicated",
        {"tool_ids": [ID_A, ID_B], "knowledge_base_ids": [ID_A]},
        body={"tools": [f"KB-{ID_A.upper()}", f"TL-{ID_B}"]},
        org={"call_defaults": {"tools": [f"TL-{ID_A}", f"KB-{ID_A}"]}},
    ),
    Case(
        "inline_tool_name_clash_request_wins",
        {"inline_tools": [{**_inline("lookup", "https://new.acme.test"), "method": "POST"}]},
        body={"tools": [_inline("lookup", "https://new.acme.test")]},
        org={"call_defaults": {"tools": [_inline("lookup", "https://old.acme.test")]}},
    ),
    Case(
        "knowledge_base_alias",
        {"knowledge_base_ids": [ID_B], "tool_ids": []},
        body={"knowledge_base_ids": [f"KB-{ID_B}"]},
    ),
    # dispositions
    Case(
        "request_dispositions_replace_org",
        {"dispositions": ["PROMOTER", *BUILTINS]},
        body={"dispositions": ["PROMOTER"]},
        org={"dispositions_default": ["Hot lead"]},
    ),
    Case(
        "builtins_not_duplicated",
        {"dispositions": ["INTERESTED", "PROMOTER", *BUILTINS[1:]]},
        body={"available_tags": ["INTERESTED", "PROMOTER"]},
    ),
    # guard rails
    Case(
        "guard_rails_union_request_wins",
        {
            "guard_rails": [
                rail("tcpa:opt_out", action={"type": "none", "phone_number": None, "node_id": None}),
                rail("tcpa:self_introduction"),
                rail("custom", name="calm", description="Stay calm"),
                AI_RAIL,
            ]
        },
        body={
            "guard_rails": [
                {"type": "tcpa:opt_out", "action": {"type": "none"}},
                {"type": "custom", "name": "calm", "description": "Stay calm"},
            ]
        },
        org={
            "default_guard_rails": [
                {"type": "tcpa:opt_out", "action": {"type": "end_call"}},
                {"type": "tcpa:self_introduction"},
            ]
        },
    ),
    Case(
        "configured_ai_rail_not_duplicated",
        {"guard_rails": [rail("tcpa:ai_disclosure", config={"mode": "check"})]},
        body={"guard_rails": [{"type": "tcpa:ai_disclosure", "config": {"mode": "check"}}]},
    ),
    Case("ai_policy_never", {"guard_rails": []}, org={"compliance": {"ai_disclosure_policy": "never"}}),
    Case(
        "ai_policy_auto_other_state",
        {"guard_rails": []},
        org={"compliance": {"ai_disclosure_policy": "auto"}},
    ),
    Case(
        "ai_policy_auto_disclosure_state",
        {"guard_rails": [AI_RAIL]},
        org={"compliance": {"ai_disclosure_policy": "auto"}},
        refs={"ai_disclosure_state": True},
    ),
    Case(
        "ai_required_beats_policy_never",
        {"guard_rails": [AI_RAIL]},
        org={"ai_disclosure_required": True, "compliance": {"ai_disclosure_policy": "never"}},
    ),
    Case(
        "recording_auto_one_party_state",
        {"guard_rails": []},
        body={"record": True},
        org={"recording_disclosure_required": False, "compliance": {"ai_disclosure_policy": "never"}},
    ),
    Case(
        "recording_auto_all_party_state",
        {"guard_rails": [RECORDING_RAIL]},
        body={"record": True},
        org={"recording_disclosure_required": False, "compliance": {"ai_disclosure_policy": "never"}},
        refs={"all_party_consent": True},
    ),
    Case(
        "recording_policy_always",
        {"guard_rails": [RECORDING_RAIL]},
        body={"record": True},
        org={
            "recording_disclosure_required": False,
            "compliance": {"ai_disclosure_policy": "never", "recording_disclosure_policy": "always"},
        },
    ),
    Case(
        "recording_rail_needs_a_recorded_call",
        {"guard_rails": []},
        org={"compliance": {"ai_disclosure_policy": "never", "recording_disclosure_policy": "always"}},
    ),
    Case(
        "citations_force_record",
        {"record": True, "citation_schema_ids": [ID_A]},
        body={"record": False, "citation_schema_ids": [ID_A]},
    ),
    # model mapping
    Case("model_base", {"dialogue_model": DIALOGUE, "router_model": ROUTER}, body={"model": "base"}),
    Case("model_enhanced", {"dialogue_model": DIALOGUE}, body={"model": "enhanced"}),
    Case("model_turbo", {"dialogue_model": ROUTER, "router_model": ROUTER}, body={"model": "turbo"}),
    Case(
        "model_base_uses_org_llm_models",
        {"dialogue_model": "acme/dialogue", "router_model": "acme/router"},
        org={"llm": {"dialogue_model": "acme/dialogue", "router_model": "acme/router"}},
    ),
    Case(
        "model_explicit_org_model_allowed",
        {"dialogue_model": "acme/analysis"},
        body={"model": "acme/analysis"},
        org={"llm": {"analysis_model": "acme/analysis"}},
    ),
    Case("model_explicit_platform_id", {"dialogue_model": ROUTER}, body={"model": ROUTER}),
    Case(
        "model_override_allowed",
        {"dialogue_model": "vendor/any-model"},
        body={"model": "vendor/any-model"},
        org={"llm": {"allow_model_override": True}},
    ),
    # renames and start_time
    Case(
        "renamed_parameters",
        {"interruption_threshold_ms": 300, "max_duration_min": 0.5},
        body={"interruption_threshold": 300, "max_duration": 0.5},
    ),
    Case(
        "start_time_naive_in_dst_zone",
        {"start_time": "2026-11-30T22:00:00Z", "timezone": "Australia/Sydney"},
        body={"start_time": "2026-12-01 09:00:00", "timezone": "Australia/Sydney"},
    ),
    Case(
        "start_time_naive_in_org_timezone",
        {"start_time": "2026-11-20T14:30:00Z"},
        body={"start_time": "2026-11-20 09:30:00"},
        org={"timezone": "America/New_York"},
    ),
    Case(
        "start_time_offset_form",
        {"start_time": "2026-11-20T14:30:00Z", "timezone": "America/Los_Angeles"},
        body={"start_time": "2026-11-20 09:30:00 -05:00"},
    ),
]


def _subset(actual: Any, expected: Any) -> Any:
    """`actual` reduced to the keys of `expected` (recursively for objects and equal-length lists)."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        return {key: _subset(actual.get(key), value) for key, value in expected.items()}
    if isinstance(expected, list) and isinstance(actual, list) and len(actual) == len(expected):
        return [_subset(item, wanted) for item, wanted in zip(actual, expected, strict=True)]
    return actual


def test_the_table_has_at_least_forty_cases() -> None:
    assert len(CASES) >= 40
    assert len({case.id for case in CASES}) == len(CASES)


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_precedence(case: Case) -> None:
    body = {key: value for key, value in {**BASE_BODY, **case.body}.items() if value is not None}
    config = dumped(resolve(body, org=case.org, runner=case.runner, refs=case.refs))
    assert _subset(config, case.expected) == case.expected
