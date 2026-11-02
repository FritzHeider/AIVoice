"""`inbound_request` (specs/16 §4, Decision P05-T12a): an inbound number's config as the Send Call request."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from va_domain.call_resolution import TaskRunner, default_inbound_prompt, inbound_request, merge_layers
from va_domain.calls import CallRequestError, SendCallRequest
from va_domain.numbers import ROUTING_FIELDS, load_inbound_config
from va_domain.org import DEFAULT_INBOUND_PROMPT, OrgSettings, load_settings

CALLER = "+14155550123"
CALLED = "+14155550199"
PATHWAY = uuid.UUID("0190f1d2-7a3b-7c4d-8e5f-0123456789ab")
PERSONA = uuid.UUID("0190f1d2-7a3b-7c4d-8e5f-0123456789ac")


def _request(
    stored: dict[str, Any],
    *,
    org: OrgSettings | None = None,
    caller: str = CALLER,
    org_name: str = "Bella Pasta",
) -> SendCallRequest:
    return inbound_request(
        load_inbound_config(stored),
        caller=caller,
        called=CALLED,
        org=org or load_settings({}),
        org_name=org_name,
    )


def test_prompt_becomes_the_task() -> None:
    request = _request({"prompt": "Take reservations for {{org_name}}."})
    assert request.task == "Take reservations for {{org_name}}."
    assert request.runner_type == "task"


def test_caller_and_called_number() -> None:
    request = _request({"prompt": "Hi"})
    assert (request.phone_number, request.from_) == (CALLER, CALLED)


def test_anonymous_callers_are_kept_as_sent() -> None:
    assert _request({"prompt": "Hi"}, caller="anonymous").phone_number == "anonymous"


def test_no_runner_answers_with_the_default_prompt() -> None:
    request = _request({}, org_name="Bella Pasta")
    assert request.task == "You are a helpful receptionist for Bella Pasta. Take a message."
    assert DEFAULT_INBOUND_PROMPT.replace("{{org_name}}", "Bella Pasta") == request.task


def test_a_custom_default_prompt_fills_only_the_org_name() -> None:
    org = load_settings({"default_inbound_prompt": "{{org_name}} desk. Ask for {{order_id}}."})
    assert _request({}, org=org, org_name="Acme").task == "Acme desk. Ask for {{order_id}}."
    assert default_inbound_prompt(org, "Acme") == "Acme desk. Ask for {{order_id}}."


def test_routing_fields_are_not_request_parameters() -> None:
    stored = {
        "prompt": "Hi",
        "fallback_number": "+14155550100",
        "maintenance": {"enabled": True},
        "allowed_callers": [CALLER],
        "blocked_callers": ["+14155550111"],
        "block_dnc_callers": True,
        "byo_original_connection_id": "1684641123236054244",
    }
    request = _request(stored)
    assert set(request.model_fields_set).isdisjoint(ROUTING_FIELDS)
    assert set(request.explicit_fields()) == {"from"}


def test_unset_fields_stay_unset_so_org_defaults_win() -> None:
    org = load_settings({"default_voice": "Nova", "default_record": True})
    request = _request({"prompt": "Hi", "temperature": 0.3}, org=org)
    assert request.explicit_fields() == {"temperature": 0.3, "from": CALLED}
    merged = merge_layers(org, request, TaskRunner())
    assert merged.voice is None  # the loader falls back to the org default voice
    assert merged.values["record"] is True
    assert merged.values["temperature"] == 0.3


def test_set_fields_override_org_defaults() -> None:
    org = load_settings({"default_voice": "Nova"})
    request = _request({"prompt": "Hi", "voice": "Karen", "voicemail": {"action": "hangup"}}, org=org)
    merged = merge_layers(org, request, TaskRunner())
    assert merged.voice == "Karen"
    assert request.explicit_fields()["voicemail"] == {"action": "hangup"}


def test_pathway_and_persona_runner() -> None:
    request = _request({"pathway_id": str(PATHWAY), "pathway_version": "staging", "persona_id": str(PERSONA)})
    assert request.task is None
    assert (request.pathway_id, request.pathway_version, request.persona_id) == (PATHWAY, "staging", PERSONA)
    assert request.runner_type == "persona"


def test_an_unusable_caller_is_invalid_input() -> None:
    with pytest.raises(CallRequestError) as caught:
        _request({"prompt": "Hi"}, caller="")
    assert (caught.value.code, caught.value.field) == ("INVALID_INPUT", "phone_number")
