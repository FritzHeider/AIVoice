"""Layer parsing, rule failures, warnings and `merge_layers` (specs/05-calls-api.md §4.3, P05-T03b)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from resolution_helpers import (
    AGENT_ID,
    AGENT_VERSION_ID,
    BASE_BODY,
    NOW,
    PATHWAY_ID,
    PERSONA_ID,
    dumped,
    pathway_ref,
    resolve,
)

from va_domain.call_resolution import (
    MAX_SCHEDULE_AHEAD,
    AgentRunner,
    PathwayRunner,
    PersonaRunner,
    ResolutionWarning,
    TaskRunner,
    check_call_defaults,
    merge_layers,
    persona_layer,
)
from va_domain.calls import CallRequestError, DialingStrategy, InlineTool, SendCallRequest
from va_domain.org import load_settings

KB = "KB-0192f5a1-7c3e-7d4a-9b1c-2f3e4d5c6d0a"
PATHWAY_BODY: dict[str, Any] = {"phone_number": "+14155550100", "pathway_id": str(PATHWAY_ID)}


def _error(body: dict[str, Any], **kwargs: Any) -> CallRequestError:
    with pytest.raises(CallRequestError) as caught:
        resolve(body, **kwargs)
    return caught.value


@pytest.mark.parametrize(
    ("org", "runner", "body", "field"),
    [
        ({"call_defaults": {"recrod": True}}, None, {}, "call_defaults.recrod"),
        ({"call_defaults": {"max_tokens": 5}}, None, {}, "call_defaults.max_tokens"),
        ({"call_defaults": {"phone_number": "+1"}}, None, {}, "call_defaults.phone_number"),
        ({"call_defaults": {"test_mode": True}}, None, {}, "call_defaults.test_mode"),
        ({"call_defaults": {"start_time": "2026-11-20 09:00:00"}}, None, {}, "call_defaults.start_time"),
        ({"call_defaults": {"retry": {"max_attempts": 2}}}, None, {}, "call_defaults.retry.wait"),
        ({"call_defaults": {"no_input_timeout_ms": 100}}, None, {}, "call_defaults.no_input_timeout_ms"),
        ({"call_defaults": {"tools": "nope"}}, None, {}, "call_defaults.tools"),
        ({"default_language": "xx-YY"}, None, {}, "default_language"),
        ({"default_webhook_events": ["call", "nope"]}, None, {}, "default_webhook_events[1]"),
        ({"default_guard_rails": [{"type": "custom", "name": "x"}]}, None, {}, "default_guard_rails[0]"),
        (
            {"default_guard_rails": [{"type": "tcpa:opt_out", "action": {"type": "jump"}}]},
            None,
            {},
            "default_guard_rails[0].action.type",
        ),
        (
            {},
            PathwayRunner(pathway_ref(call_defaults={"task": "x"})),
            {**PATHWAY_BODY, "task": None},
            "pathway.call_defaults.task",
        ),
        (
            {},
            AgentRunner(AGENT_ID, AGENT_VERSION_ID, {"max_tokens": 1}),
            {"task": None, "agent_id": str(AGENT_ID)},
            "agent.call_settings.max_tokens",
        ),
        (
            {},
            PersonaRunner(PERSONA_ID, 1, False, {"call_settings": {"temperature": 3}}),
            {"task": None, "persona_id": str(PERSONA_ID)},
            "persona.config.call_settings.temperature",
        ),
        (
            {},
            PersonaRunner(PERSONA_ID, 1, False, {"memory": {"enabled": "maybe"}}),
            {"task": None, "persona_id": str(PERSONA_ID)},
            "persona.config.memory.enabled",
        ),
        (
            {},
            PersonaRunner(PERSONA_ID, 1, False, {"prompt": "P"}),
            {"task": None, "persona_id": str(PERSONA_ID), "persona_settings": {"max_tokens": 9}},
            "persona_settings.max_tokens",
        ),
    ],
)
def test_invalid_layer_names_its_source(
    org: dict[str, Any], runner: Any, body: dict[str, Any], field: str
) -> None:
    merged = {key: value for key, value in {**BASE_BODY, **body}.items() if value is not None}
    error = _error(merged, org=org, runner=runner)
    assert (error.code, error.field) == ("INVALID_INPUT", field)


def test_model_outside_the_allowlist_is_rejected() -> None:
    error = _error({**BASE_BODY, "model": "vendor/other"}, org={"llm": {"dialogue_model": "acme/d"}})
    assert (error.code, error.field) == ("INVALID_INPUT", "model")
    assert error.message == "Model 'vendor/other' is not allowed for this organization."


@pytest.mark.parametrize(
    ("offset", "message"),
    [
        (timedelta(0), "start_time must be in the future."),
        (-timedelta(minutes=1), "start_time must be in the future."),
        (MAX_SCHEDULE_AHEAD + timedelta(seconds=1), "start_time must be at most 90 days ahead."),
    ],
)
def test_start_time_window_is_enforced(offset: timedelta, message: str) -> None:
    error = _error({**BASE_BODY, "start_time": (NOW + offset).isoformat()})
    assert (error.code, error.field, error.message) == ("INVALID_INPUT", "start_time", message)


def test_start_time_exactly_ninety_days_ahead_is_accepted() -> None:
    config = resolve({**BASE_BODY, "start_time": (NOW + MAX_SCHEDULE_AHEAD).isoformat()}).config
    assert config.start_time == NOW + MAX_SCHEDULE_AHEAD


def test_naive_now_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        resolve(BASE_BODY, now=datetime(2026, 11, 15, 12, 0))  # noqa: DTZ001 - the naive value is the point


def test_runner_inputs_must_match_the_request() -> None:
    with pytest.raises(ValueError, match="runner inputs for 'pathway'"):
        resolve(BASE_BODY, runner=PathwayRunner(pathway_ref()))


def test_pathway_runner_warns_about_ignored_task_and_transfers() -> None:
    body = {**PATHWAY_BODY, "task": "Hi", "transfer_phone_number": "+14155550199"}
    runner = PathwayRunner(pathway_ref(call_defaults={"transfer_list": {"default": "+14155550198"}}))
    resolution = resolve(body, runner=runner)
    assert [(w.code, w.field) for w in resolution.warnings] == [
        ("TASK_IGNORED", "task"),
        ("TRANSFER_IGNORED", "transfer_phone_number"),
        ("TRANSFER_IGNORED", "transfer_list"),
    ]
    config = dumped(resolution)
    assert (config["task"], config["transfer_phone_number"], config["transfer_list"]) == (None, None, {})


def test_task_runner_keeps_transfers_without_warnings() -> None:
    resolution = resolve({**BASE_BODY, "transfer_phone_number": "+14155550199"})
    assert resolution.warnings == ()
    assert resolution.config.transfer_phone_number == "+14155550199"


def test_agent_pathway_drops_the_agent_task() -> None:
    runner = AgentRunner(AGENT_ID, AGENT_VERSION_ID, {"task": "Agent"}, pathway_ref())
    resolution = resolve({"phone_number": "+14155550100", "agent_id": str(AGENT_ID)}, runner=runner)
    assert (resolution.config.task, resolution.config.pathway_version) == (None, 3)
    assert resolution.warnings == ()


def test_warning_carries_a_message() -> None:
    resolution = resolve({**PATHWAY_BODY, "task": "Hi"}, runner=PathwayRunner(pathway_ref()))
    assert resolution.warnings == (
        ResolutionWarning("TASK_IGNORED", "task is ignored because the call runs a pathway.", "task"),
    )


def test_check_call_defaults_accepts_parameters_and_derived_fields() -> None:
    check_call_defaults({"record": True, "amd": True, "no_input_timeout_ms": 5000, "from": "+14155550190"})


@pytest.mark.parametrize(
    ("raw", "field"),
    [
        ({"foo": 1}, "call_defaults.foo"),
        ({"task": "x"}, "call_defaults.task"),
        ({"pathway_id": "0192f5a1-7c3e-7d4a-9b1c-2f3e4d5c6d0a"}, "call_defaults.pathway_id"),
        ({"test_call": True}, "call_defaults.test_call"),
        ({"voicemail": {"action": "beep"}}, "call_defaults.voicemail.action"),
    ],
)
def test_check_call_defaults_rejects(raw: dict[str, Any], field: str) -> None:
    with pytest.raises(CallRequestError) as caught:
        check_call_defaults(raw)
    assert caught.value.field == field


def test_persona_layer_maps_the_persona_config() -> None:
    layer = persona_layer(
        {
            "name": "Maya",
            "runner": "prompt",
            "prompt": "Help callers.",
            "first_sentence": "Hi!",
            "voice": "Maya",
            "language": "es",
            "knowledge_base_ids": [KB],
            "memory": {"enabled": True, "scope": "persona"},
            "call_settings": {"record": True, "max_duration": 10, "model": "turbo", "timezone": None},
            "dispositions": [],
            "summary_prompt": None,
            "metadata": {"team": "a"},
            "sms": {"objective": "x"},
            "routing": [{"when": "always"}],
        }
    )
    assert layer == {
        "first_sentence": "Hi!",
        "voice": "Maya",
        "language": "es",
        "tools": [KB],
        "memory_enabled": True,
        "record": True,
        "max_duration": 10.0,
        "model": "turbo",
        "metadata": {"team": "a"},
    }


def test_merge_layers_exposes_what_the_loader_needs() -> None:
    org = load_settings({"call_defaults": {"voice": "Org voice", "country": "CA", "tools": [KB]}})
    body = SendCallRequest.model_validate(
        {
            **BASE_BODY,
            "from": "+14155550190",
            "local_dialing": True,
            "tools": [{"name": "t", "description": "d", "url": "https://a.test"}],
        }
    )
    merged = merge_layers(org, body, TaskRunner())
    assert (merged.voice, merged.from_, merged.country) == ("Org voice", "+14155550190", "CA")
    assert merged.dialing_strategy == DialingStrategy(type="local")
    assert merged.tools[0] == KB
    assert isinstance(merged.tools[1], InlineTool)
    assert (merged.timezone, merged.language, merged.task, merged.pathway) == (
        "America/Los_Angeles",
        "babel-en",
        "Hi",
        None,
    )


def test_merge_layers_defaults_without_any_layer() -> None:
    merged = merge_layers(load_settings({}), SendCallRequest.model_validate(BASE_BODY), TaskRunner())
    assert (merged.voice, merged.from_, merged.tools, merged.country) == (None, None, [], "US")
    assert merged.dialing_strategy == DialingStrategy()


def test_null_keyed_object_in_a_layer_changes_nothing() -> None:
    config = resolve(
        {**BASE_BODY, "warm_transfer": None},
        org={"call_defaults": {"warm_transfer": {"enabled": True}}},
    ).config
    assert config.warm_transfer.enabled is True


def test_blank_persona_prompt_leaves_no_task() -> None:
    runner = PersonaRunner(PERSONA_ID, None, True, {"prompt": "   "})
    config = resolve({"phone_number": "+14155550100", "persona_id": str(PERSONA_ID)}, runner=runner).config
    assert (config.task, config.persona_id, config.runner_type) == (None, PERSONA_ID, "persona")
