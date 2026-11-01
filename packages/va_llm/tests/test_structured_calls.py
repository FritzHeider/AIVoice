"""``LlmGateway.call_structured`` against respx (specs/10 §3; Decision 2026-11-01 P03-T14a rules 1,
4-5, 8-10)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import orjson
import pytest
import respx
from llm_client_helpers import (
    COMPLETIONS,
    ORG_COMPLETIONS,
    GatewayRig,
    StaticOrgProviders,
    completion_body,
    gateway_rig,
    make_provider,
    org_provider,
    sent_json,
    user,
)
from service_helpers import json_log_stream, log_events
from structured_models import Answer, Loop, Route

from va_llm import (
    LlmUnavailableError,
    OrgProviderError,
    StructuredOutputError,
    register_structured_function,
    tool_schema,
)
from va_llm.providers import OrgProviderSource, org_provider_id

VALID_LOOP = '{"score": 8, "met": true, "missing": ""}'
INVALID_LOOP = '{"score": 11, "met": true, "missing": ""}'
OVER_MAX = "score: 11 is greater than the maximum of 10"
REPAIR = f"Your previous output was invalid: {OVER_MAX}. Call score_condition again with corrected arguments."


def _tool_call(arguments: str, name: str = "score_condition", call_id: str = "call_1") -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def _forced(*calls: dict[str, Any], content: str | None = None) -> httpx.Response:
    return httpx.Response(
        200, json=completion_body(content, tool_calls=list(calls), finish_reason="tool_calls")
    )


def _content(text: str | None) -> httpx.Response:
    return httpx.Response(200, json=completion_body(text))


async def _score(rig: GatewayRig, **kwargs: Any) -> Loop:
    return await rig.gateway.call_structured(
        "loop_condition",
        Loop,
        org_id=rig.org_id,
        messages=[user("Is the condition met?")],
        function_name="score_condition",
        description="Score the loop condition.",
        **kwargs,
    )


def _sent(route: respx.Route) -> list[dict[str, Any]]:
    return [sent_json(call.request) for call in route.calls]


async def test_a_valid_forced_tool_call(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    route = llm_api.post(COMPLETIONS).mock(return_value=_forced(_tool_call(VALID_LOOP)))
    result = await _score(rig)
    assert result == Loop(score=8, met=True, missing="")
    [body] = _sent(route)
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "score_condition",
                "description": "Score the loop condition.",
                "parameters": tool_schema(Loop),
            },
        }
    ]
    assert body["tool_choice"] == {"type": "function", "function": {"name": "score_condition"}}
    assert (body["model"], body["temperature"], body["max_tokens"]) == ("rtr-model", 0.0, 512)
    assert "stream" not in body
    assert body["messages"] == [{"role": "user", "content": "Is the condition met?"}]
    assert rig.metric("va_llm_structured_total", function="score_condition", outcome="valid") == 1
    assert len(rig.usage.records) == 1


async def test_temperature_tokens_and_call_id_pass_through(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    route = llm_api.post(COMPLETIONS).mock(return_value=_forced(_tool_call(VALID_LOOP)))
    call_id = uuid.uuid4()
    await _score(rig, temperature=0.3, max_tokens=64, call_id=call_id)
    [call] = route.calls
    body = sent_json(call.request)
    assert (body["temperature"], body["max_tokens"]) == (0.3, 64)
    assert call.request.headers["x-va-call-id"] == str(call_id)


async def test_the_call_named_after_the_function_wins(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    llm_api.post(COMPLETIONS).mock(
        return_value=_forced(_tool_call(INVALID_LOOP, name="other"), _tool_call(VALID_LOOP, call_id="call_2"))
    )
    assert (await _score(rig)).score == 8


async def test_a_differently_named_single_call_is_used(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    llm_api.post(COMPLETIONS).mock(return_value=_forced(_tool_call(VALID_LOOP, name="scoreCondition")))
    assert (await _score(rig)).met is True


async def test_a_schema_violation_is_repaired(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    route = llm_api.post(COMPLETIONS).mock(
        side_effect=[_forced(_tool_call(INVALID_LOOP)), _forced(_tool_call(VALID_LOOP))]
    )
    assert (await _score(rig)).score == 8
    first, second = _sent(route)
    assert second["messages"] == [
        *first["messages"],
        {"role": "assistant", "content": INVALID_LOOP},
        {"role": "user", "content": REPAIR},
    ]
    assert second["tools"] == first["tools"]
    assert second["tool_choice"] == first["tool_choice"]
    assert rig.metric("va_llm_structured_total", function="score_condition", outcome="repaired") == 1
    assert rig.metric("va_llm_structured_total", function="score_condition", outcome="valid") == 0
    assert len(rig.usage.records) == 2


async def test_non_json_arguments_are_repaired(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    route = llm_api.post(COMPLETIONS).mock(
        side_effect=[_forced(_tool_call('{"score": 8,')), _forced(_tool_call(VALID_LOOP))]
    )
    assert (await _score(rig)).score == 8
    repair = _sent(route)[1]["messages"][-1]["content"]
    assert repair.startswith("Your previous output was invalid: $: arguments are not valid JSON (")


async def test_a_pydantic_only_violation_is_repaired(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    loud = '{"score": 2, "met": false, "missing": "NAME"}'
    route = llm_api.post(COMPLETIONS).mock(
        side_effect=[_forced(_tool_call(loud)), _forced(_tool_call(VALID_LOOP))]
    )
    assert (await _score(rig)).score == 8
    assert _sent(route)[1]["messages"][-1]["content"] == (
        "Your previous output was invalid: missing: Value error, must not be upper case. "
        "Call score_condition again with corrected arguments."
    )


async def test_a_second_invalid_output_raises(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    stream = json_log_stream()
    route = llm_api.post(COMPLETIONS).mock(
        side_effect=[_forced(_tool_call(INVALID_LOOP)), _forced(_tool_call('{"score": -1, "met": 1}'))]
    )
    with pytest.raises(StructuredOutputError) as caught:
        await _score(rig)
    error = caught.value
    assert (error.function_name, error.attempts, error.provider_id, error.model) == (
        "score_condition",
        2,
        "platform",
        "rtr-model",
    )
    assert error.errors == (
        "$: 'missing' is a required property",
        "met: 1 is not of type 'boolean'",
        "score: -1 is less than the minimum of 0",
    )
    assert error.error_kind == "parse"
    assert len(route.calls) == 2
    assert len(rig.usage.records) == 2
    assert rig.metric("va_llm_structured_total", function="score_condition", outcome="invalid") == 1
    events = [event for event in log_events(stream) if event["event"] == "llm.structured_invalid"]
    assert [(e["function"], e["attempt"], e["errors"], e["level"]) for e in events] == [
        ("score_condition", 1, 1, "warning"),
        ("score_condition", 2, 3, "warning"),
    ]
    assert all("arguments" not in event and INVALID_LOOP not in str(event) for event in events)


async def test_json_in_content_without_a_tool_call_is_accepted(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    route = llm_api.post(COMPLETIONS).mock(return_value=_content(f"Here you go: {VALID_LOOP}"))
    assert (await _score(rig)).score == 8
    assert len(route.calls) == 1


async def test_no_tool_call_and_no_json_repairs_in_json_mode(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    route = llm_api.post(COMPLETIONS).mock(
        side_effect=[_content("I think it is met."), _content(f"```json\n{VALID_LOOP}\n```")]
    )
    assert (await _score(rig)).score == 8
    first, second = _sent(route)
    assert "tools" in first
    assert "tools" not in second
    assert "tool_choice" not in second
    instruction = second["messages"][1]
    assert instruction["role"] == "system"
    assert instruction["content"].startswith(
        "Respond with only a JSON object that matches this JSON Schema: "
    )
    assert orjson.loads(instruction["content"].split("JSON Schema: ", 1)[1]) == tool_schema(Loop)
    assert second["messages"][0] == first["messages"][0]
    assert second["messages"][2:] == [
        {"role": "assistant", "content": "I think it is met."},
        {
            "role": "user",
            "content": "Your previous output was invalid: $: no tool call and no JSON object in the reply. "
            "Call score_condition again with corrected arguments.",
        },
    ]


async def test_invalid_json_in_content_repairs_in_json_mode(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    route = llm_api.post(COMPLETIONS).mock(side_effect=[_content(INVALID_LOOP), _content(VALID_LOOP)])
    assert (await _score(rig)).score == 8
    second = _sent(route)[1]
    assert "tools" not in second
    assert second["messages"][-2:] == [
        {"role": "assistant", "content": INVALID_LOOP},
        {"role": "user", "content": REPAIR},
    ]


async def test_an_empty_reply_in_json_mode_fails_after_the_repair(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    llm_api.post(COMPLETIONS).mock(side_effect=[_content(None), _content("still nothing")])
    with pytest.raises(StructuredOutputError) as caught:
        await _score(rig)
    assert caught.value.errors == ("$: no JSON object in the reply",)


@pytest.fixture
async def json_mode_rig() -> AsyncIterator[GatewayRig]:
    async with gateway_rig(platform=make_provider(supports_tool_choice=False)) as rig:
        yield rig


async def test_a_provider_without_tool_choice_uses_json_mode(
    json_mode_rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    route = llm_api.post(COMPLETIONS).mock(return_value=_content(f"{{oops}} {VALID_LOOP}"))
    assert (await _score(json_mode_rig)).score == 8
    [body] = _sent(route)
    assert "tools" not in body
    assert "tool_choice" not in body
    assert [message["role"] for message in body["messages"]] == ["user", "system"]


async def test_json_mode_repair_keeps_json_mode(json_mode_rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    route = llm_api.post(COMPLETIONS).mock(side_effect=[_content(INVALID_LOOP), _content(VALID_LOOP)])
    assert (await _score(json_mode_rig)).score == 8
    second = _sent(route)[1]
    assert [message["role"] for message in second["messages"]] == ["user", "system", "assistant", "user"]
    assert "tools" not in second


async def test_the_org_provider_decides_the_mode(llm_api: respx.MockRouter) -> None:
    org_id = uuid.uuid4()
    provider = org_provider(org_id).model_copy(update={"supports_tool_choice": False})
    async with gateway_rig(org_providers=StaticOrgProviders({org_id: provider})) as rig:
        route = llm_api.post(ORG_COMPLETIONS).mock(return_value=_content(VALID_LOOP))
        result = await rig.gateway.call_structured(
            "loop_condition",
            Loop,
            org_id=org_id,
            messages=[user()],
            function_name="score_condition",
            description="d",
        )
    assert result.score == 8
    [body] = _sent(route)
    assert (body["model"], "tools" in body) == ("org-rtr-model", False)


class _BrokenOrgProviders:
    async def get(self, org_id: uuid.UUID) -> None:
        raise OrgProviderError(provider_id=org_provider_id(org_id), cause="OperationalError")


async def test_an_org_provider_failure_propagates_unavailable(llm_api: respx.MockRouter) -> None:
    broken: OrgProviderSource = _BrokenOrgProviders()
    async with gateway_rig(org_providers=broken) as rig:
        route = llm_api.post(COMPLETIONS).mock(return_value=_forced(_tool_call(VALID_LOOP)))
        with pytest.raises(LlmUnavailableError) as caught:
            await _score(rig)
    assert caught.value.reason == "provider_unavailable"
    assert route.calls.call_count == 0


async def test_an_org_provider_failure_uses_the_platform_mode_for_analysis(llm_api: respx.MockRouter) -> None:
    register_structured_function("classify_answer", Answer)
    broken: OrgProviderSource = _BrokenOrgProviders()
    async with gateway_rig(org_providers=broken) as rig:
        route = llm_api.post(COMPLETIONS).mock(
            return_value=_forced(_tool_call('{"label": "human", "confidence": 0.9}', name="classify_answer"))
        )
        result = await rig.gateway.call_structured(
            "analysis",
            Answer,
            org_id=rig.org_id,
            messages=[user()],
            function_name="classify_answer",
            description="d",
        )
    assert result.label == "human"
    [body] = _sent(route)
    assert (body["model"], body["tool_choice"]["function"]["name"]) == ("fb-model", "classify_answer")


async def test_unavailable_propagates_unchanged(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    route = llm_api.post(COMPLETIONS).mock(
        return_value=httpx.Response(400, json={"error": {"message": "bad"}})
    )
    with pytest.raises(LlmUnavailableError) as caught:
        await _score(rig)
    assert caught.value.reason == "exhausted"
    assert len(route.calls) == 1
    assert (
        rig.registry.get_sample_value(
            "va_llm_structured_total", {"function": "score_condition", "outcome": "invalid"}
        )
        is None
    )


async def test_unavailable_during_the_repair_propagates(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    llm_api.post(COMPLETIONS).mock(
        side_effect=[_forced(_tool_call(INVALID_LOOP)), httpx.Response(400, json={"error": {"message": "x"}})]
    )
    with pytest.raises(LlmUnavailableError):
        await _score(rig)


async def test_dialogue_is_refused(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    route = llm_api.post(COMPLETIONS).mock(return_value=_forced(_tool_call(VALID_LOOP)))
    with pytest.raises(ValueError, match="dialogue"):
        await rig.gateway.call_structured(
            "dialogue",
            Loop,
            org_id=rig.org_id,
            messages=[user()],
            function_name="score_condition",
            description="",
        )
    assert route.calls.call_count == 0


async def test_unknown_or_conflicting_names_are_refused(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    route = llm_api.post(COMPLETIONS).mock(return_value=_forced(_tool_call(VALID_LOOP)))
    with pytest.raises(ValueError, match="not a registered structured function name"):
        await rig.gateway.call_structured(
            "router", Loop, org_id=rig.org_id, messages=[user()], function_name="pick_path", description=""
        )
    register_structured_function("select_path", Route)
    with pytest.raises(ValueError, match="registered with Route, not Loop"):
        await rig.gateway.call_structured(
            "router", Loop, org_id=rig.org_id, messages=[user()], function_name="select_path", description=""
        )
    assert route.calls.call_count == 0


async def test_a_registered_schema_is_accepted(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    register_structured_function("score_condition", Loop)
    llm_api.post(COMPLETIONS).mock(return_value=_forced(_tool_call(VALID_LOOP)))
    assert (await _score(rig)).met is True
