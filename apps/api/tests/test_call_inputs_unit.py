"""Send Call input loading, pure parts: step-4 permission order, caller-ID ordering, destination parsing."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from call_input_rows import error_of, make_ctx, request_of

from va_api.services.call_inputs import app_error, check_call_permissions
from va_common.auth import KeyEnvironment
from va_common.errors import AppError
from va_common.settings import Environment
from va_db.call_inputs.caller_id import Candidate, local_order, parse_destination
from va_db.call_inputs.pathways import start_node_of
from va_domain.calls import CallRequestError

ORG = uuid.UUID("0192f5a1-7c3e-7d4a-9b1c-2f3e4d5c6e01")
AGENT = "0192f5a1-7c3e-7d4a-9b1c-2f3e4d5c6e02"
PATHWAY = "0192f5a1-7c3e-7d4a-9b1c-2f3e4d5c6e03"


def _check(
    body: dict[str, Any],
    *,
    scopes: tuple[str, ...] = ("*",),
    environment: KeyEnvironment = "live",
    va_env: Environment = Environment.TEST,
) -> tuple[str, str | None, str]:
    with pytest.raises(AppError) as caught:
        check_call_permissions(
            request_of(body), make_ctx(ORG, scopes=scopes, environment=environment), va_env=va_env
        )
    return error_of(caught.value)


def test_request_rules_come_first() -> None:
    body = {"retry": {"voicemail_action": "hangup"}, "agent_id": AGENT, "task": None, "test_mode": True}
    assert _check(body, scopes=("calls:write",))[:2] == ("RETRY_NOT_PROVIDED", "retry.wait")
    conflict = {"agent_id": AGENT, "pathway_id": PATHWAY, "task": None, "test_mode": True}
    assert _check(conflict, scopes=("calls:write",))[:2] == ("RUNNER_CONFLICT", "agent_id")


def test_agent_requires_an_org_scoped_key_before_test_permissions() -> None:
    body = {"agent_id": AGENT, "task": None, "test_call": True, "test_mode": True}
    assert _check(body, scopes=("calls:write",)) == (
        "AGENT_REQUIRES_ORG_KEY",
        "agent_id",
        "agent_id requires an org-scoped API key (agents:read or agents:write).",
    )
    for scopes in (("calls:write", "agents:read"), ("calls:write", "agents:write"), ("*",)):
        check_call_permissions(
            request_of({"agent_id": AGENT, "task": None}),
            make_ctx(ORG, scopes=scopes),
            va_env=Environment.TEST,
        )


def test_test_call_needs_pathways_write_then_test_mode_needs_a_test_key() -> None:
    body = {"test_call": True, "test_mode": True}
    assert _check(body, scopes=("calls:write",)) == (
        "FORBIDDEN",
        "test_call",
        "test_call requires the pathways:write scope or a developer role.",
    )
    assert _check(body, scopes=("calls:write", "pathways:write")) == (
        "FORBIDDEN",
        "test_mode",
        "test_mode requires a test environment API key.",
    )


def test_test_keys_place_calls_only_in_local_and_test_deployments() -> None:
    message = "Test keys cannot place live calls, send SMS, or purchase numbers."
    for va_env in (Environment.STAGING, Environment.PRODUCTION):
        assert _check({"test_mode": True}, environment="test", va_env=va_env) == ("FORBIDDEN", None, message)
    for va_env in (Environment.LOCAL, Environment.TEST):
        check_call_permissions(
            request_of({"test_mode": True}), make_ctx(ORG, environment="test"), va_env=va_env
        )
    check_call_permissions(request_of({}), make_ctx(ORG), va_env=Environment.PRODUCTION)


def test_app_error_keeps_code_message_and_field() -> None:
    error = app_error(CallRequestError("INVALID_INPUT", "bad", "voicemail.message"))
    assert (error.code, error.message, error.field, error.http_status) == (
        "INVALID_INPUT",
        "bad",
        "voicemail.message",
        400,
    )


def _numbers(*e164s: str) -> list[Candidate]:
    return [Candidate(uuid.uuid4(), e164) for e164 in e164s]


def test_local_order_prefers_area_code_then_state_then_e164() -> None:
    candidates = _numbers("+12125550100", "+16505550100", "+14155550199", "+14155550101", "+13105550100")
    ordered = [c.e164 for c in local_order(candidates, "+14155550123")]
    assert ordered == ["+14155550101", "+14155550199", "+13105550100", "+16505550100", "+12125550100"]


def test_local_order_for_a_non_nanp_destination_is_by_e164() -> None:
    candidates = _numbers("+14155550100", "+12125550100", "+442071234567")
    assert [c.e164 for c in local_order(candidates, "+442079460000")] == [
        "+12125550100",
        "+14155550100",
        "+442071234567",
    ]
    assert local_order([], "+14155550123") == []


def test_parse_destination() -> None:
    assert parse_destination("(415) 555-0100", "US") == parse_destination("+14155550100", "GB")
    destination = parse_destination("020 7946 0000", "GB")
    assert (destination.e164, destination.region) == ("+442079460000", "GB")
    assert parse_destination("+18005550100", "US").region == "US"
    with pytest.raises(AppError) as caught:
        parse_destination("12", "US")
    assert error_of(caught.value) == (
        "INVALID_PHONE_NUMBER",
        "phone_number",
        "phone_number is not a valid, dialable phone number.",
    )


def test_start_node_of() -> None:
    nodes = [
        "junk",
        {"id": "a", "data": {"isStart": False}},
        {"id": "b", "data": "x"},
        {"id": "c", "data": {"isStart": True}},
    ]
    assert start_node_of(nodes) == "c"
    assert start_node_of([{"id": 7, "data": {"isStart": True}}]) is None
    assert start_node_of([]) is None
