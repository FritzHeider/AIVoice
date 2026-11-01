"""The specs/10 §2.1 retry and fallback matrix per call type (Decision P03-T13a rules 5-7), with respx."""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
import respx
from llm_client_helpers import (
    COMPLETIONS,
    ORG_COMPLETIONS,
    PLATFORM_KEY,
    GatewayRig,
    Hanging,
    StaticOrgProviders,
    completion_body,
    gateway_rig,
    hanging,
    make_provider,
    org_provider,
    scripted,
    sent_json,
    user,
)

from va_llm import (
    BreakerState,
    ChatMessage,
    CircuitBreaker,
    Completion,
    ForceTool,
    LlmConnectionError,
    LlmHttpError,
    LlmProtocolError,
    LlmRateLimitedError,
    LlmTimeoutError,
    LlmUnavailableError,
    ToolCall,
    ToolDef,
)
from va_llm.policy import CallType

OK = httpx.Response(200, json=completion_body())
CLASSIFIERS: tuple[CallType, ...] = ("router", "loop_condition", "extraction", "voicemail", "guard_rails")


def _state(breaker: CircuitBreaker) -> BreakerState:
    """The state, read through a call (mypy keeps a narrowed property across awaits)."""
    return breaker.state


def _models(route: respx.Route) -> list[str]:
    return [sent_json(call.request)["model"] for call in route.calls]


async def _complete(rig: GatewayRig, call_type: CallType, **kwargs: object) -> Completion:
    return await rig.gateway.complete(call_type, org_id=rig.org_id, messages=[user()], **kwargs)  # type: ignore[arg-type]


async def _after_timeout(
    rig: GatewayRig, route: respx.Route, call_type: CallType, seconds: float
) -> tuple[Completion, Hanging]:
    """``call_type``'s first request hangs past its ``seconds`` deadline; the next one answers OK."""
    stuck = hanging()
    route.mock(side_effect=scripted(stuck, OK))
    task = asyncio.create_task(_complete(rig, call_type))
    await rig.clock.wait_for_sleep(seconds)
    await rig.clock.run_for(seconds)
    return await task, stuck


async def _unavailable(rig: GatewayRig, call_type: CallType) -> LlmUnavailableError:
    with pytest.raises(LlmUnavailableError) as caught:
        await _complete(rig, call_type)
    return caught.value


@pytest.mark.parametrize("call_type", CLASSIFIERS)
@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_classifiers_retry_a_5xx_once_immediately(
    rig: GatewayRig, llm_api: respx.MockRouter, call_type: CallType, status: int
) -> None:
    route = llm_api.post(COMPLETIONS).mock(side_effect=[httpx.Response(status), OK])
    completion = await _complete(rig, call_type)
    assert (completion.content, completion.attempts, completion.fell_back) == ("Hello.", 2, False)
    assert _models(route) == ["rtr-model", "rtr-model"]


@pytest.mark.parametrize("call_type", CLASSIFIERS)
async def test_classifiers_give_up_after_the_retry(
    rig: GatewayRig, llm_api: respx.MockRouter, call_type: CallType
) -> None:
    route = llm_api.post(COMPLETIONS).mock(return_value=httpx.Response(503))
    error = await _unavailable(rig, call_type)
    assert route.call_count == 2
    assert error.reason == "exhausted"
    assert isinstance(error.last, LlmHttpError)
    assert error.last.status == 503
    assert error.error_kind == "http"


async def test_connect_errors_are_retried(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    route = llm_api.post(COMPLETIONS).mock(side_effect=[httpx.ConnectError("refused"), OK])
    assert (await _complete(rig, "router")).attempts == 2
    assert route.call_count == 2


async def test_two_connect_errors_exhaust_a_classifier(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    llm_api.post(COMPLETIONS).mock(side_effect=httpx.ConnectError("refused"))
    error = await _unavailable(rig, "extraction")
    assert isinstance(error.last, LlmConnectionError)


async def test_429_with_retry_after_1_s_waits_on_the_clock_then_retries(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    route = llm_api.post(COMPLETIONS).mock(
        side_effect=[httpx.Response(429, headers={"retry-after": "1"}), OK]
    )
    task = asyncio.create_task(_complete(rig, "router"))
    await rig.clock.wait_for_sleep(1.0)
    assert route.call_count == 1
    await rig.clock.run_for(1.0)
    assert (await task).attempts == 2
    assert route.call_count == 2


async def test_429_without_retry_after_retries_immediately(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    llm_api.post(COMPLETIONS).mock(side_effect=[httpx.Response(429), OK])
    assert (await _complete(rig, "voicemail")).attempts == 2


async def test_429_with_retry_after_above_1_s_is_not_retried(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    route = llm_api.post(COMPLETIONS).mock(return_value=httpx.Response(429, headers={"retry-after": "2"}))
    error = await _unavailable(rig, "router")
    assert route.call_count == 1
    assert isinstance(error.last, LlmRateLimitedError)
    assert error.last.retry_after_s == 2.0


async def test_a_retry_wait_beyond_the_budget_is_not_taken(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    slow = hanging(httpx.Response(429, headers={"retry-after": "1"}))
    route = llm_api.post(COMPLETIONS).mock(side_effect=slow)
    task = asyncio.create_task(_complete(rig, "router"))
    await rig.clock.wait_for_sleep(1.5)
    await rig.clock.run_for(0.6)
    slow.release.set()
    with pytest.raises(LlmUnavailableError) as caught:
        await task
    assert isinstance(caught.value.last, LlmRateLimitedError)
    assert route.call_count == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_other_4xx_are_never_retried(rig: GatewayRig, llm_api: respx.MockRouter, status: int) -> None:
    route = llm_api.post(COMPLETIONS).mock(
        return_value=httpx.Response(status, json={"error": {"message": "no"}})
    )
    error = await _unavailable(rig, "router")
    assert route.call_count == 1
    assert isinstance(error.last, LlmHttpError)
    assert error.last.status == status


async def test_a_malformed_body_is_not_retried(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    route = llm_api.post(COMPLETIONS).mock(return_value=httpx.Response(200, json={"choices": []}))
    error = await _unavailable(rig, "router")
    assert route.call_count == 1
    assert isinstance(error.last, LlmProtocolError)
    assert error.error_kind == "parse"


async def test_a_classifier_timeout_is_final_and_cancels_the_request(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    stuck = hanging()
    route = llm_api.post(COMPLETIONS).mock(side_effect=stuck)
    task = asyncio.create_task(_complete(rig, "router"))
    await rig.clock.wait_for_sleep(1.5)
    await rig.clock.run_for(1.5)
    with pytest.raises(LlmUnavailableError) as caught:
        await task
    assert isinstance(caught.value.last, LlmTimeoutError)
    assert caught.value.error_kind == "timeout"
    assert (route.call_count, stuck.started, stuck.cancelled) == (0, 1, 1)


async def test_the_retry_gets_only_the_rest_of_the_budget(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    stuck = hanging()
    llm_api.post(COMPLETIONS).mock(side_effect=scripted(httpx.Response(503), stuck))
    task = asyncio.create_task(_complete(rig, "guard_rails"))
    await rig.clock.wait_for_sleep(2.0)
    await rig.clock.run_for(2.0)
    with pytest.raises(LlmUnavailableError):
        await task
    assert stuck.cancelled == 1


async def test_dialogue_does_not_retry_and_falls_back_at_once(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    route = llm_api.post(COMPLETIONS).mock(side_effect=[httpx.Response(503), OK])
    completion = await _complete(rig, "dialogue")
    assert (completion.model, completion.attempts, completion.fell_back) == ("fb-model", 2, True)
    assert _models(route) == ["dlg-model", "fb-model"]


async def test_dialogue_timeout_falls_back_with_a_fresh_budget(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    route = llm_api.post(COMPLETIONS)
    completion, stuck = await _after_timeout(rig, route, "dialogue", 2.5)
    assert (completion.model, completion.fell_back) == ("fb-model", True)
    assert [sent_json(request)["model"] for request in stuck.requests] == ["dlg-model"]
    assert (_models(route), stuck.cancelled) == (["fb-model"], 1)


async def test_dialogue_with_4xx_or_slow_429_falls_back(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    route = llm_api.post(COMPLETIONS).mock(
        side_effect=[httpx.Response(400), OK, httpx.Response(429, headers={"retry-after": "5"}), OK]
    )
    assert (await _complete(rig, "dialogue")).fell_back
    assert (await _complete(rig, "dialogue")).fell_back
    assert _models(route) == ["dlg-model", "fb-model", "dlg-model", "fb-model"]


async def test_a_failing_fallback_raises_unavailable_with_its_error(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    llm_api.post(COMPLETIONS).mock(side_effect=[httpx.Response(503), httpx.Response(502)])
    error = await _unavailable(rig, "dialogue")
    assert (error.model, error.reason) == ("fb-model", "exhausted")
    assert isinstance(error.last, LlmHttpError)
    assert error.last.status == 502


async def test_analysis_retries_after_2_and_8_s_then_falls_back(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    route = llm_api.post(COMPLETIONS).mock(side_effect=[httpx.Response(500)] * 3 + [OK])
    task = asyncio.create_task(_complete(rig, "analysis"))
    await rig.clock.wait_for_sleep(2.0)
    assert route.call_count == 1
    await rig.clock.run_for(2.0)
    await rig.clock.wait_for_sleep(8.0)
    assert route.call_count == 2
    await rig.clock.run_for(8.0)
    completion = await task
    assert (completion.model, completion.attempts, completion.fell_back) == ("fb-model", 4, True)
    assert _models(route) == ["ana-model"] * 3 + ["fb-model"]


async def test_analysis_retries_timeouts_with_60_s_per_attempt(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    route = llm_api.post(COMPLETIONS)
    stuck = hanging()
    route.mock(side_effect=scripted(stuck, OK))
    task = asyncio.create_task(_complete(rig, "analysis"))
    for seconds in (60.0, 2.0):
        await rig.clock.wait_for_sleep(seconds)
        await rig.clock.run_for(seconds)
    completion = await task
    assert (completion.model, completion.attempts, completion.fell_back) == ("ana-model", 2, False)
    assert (stuck.started, stuck.cancelled, route.call_count) == (1, 1, 1)


async def test_no_fallback_when_it_is_the_primary_target(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    route = llm_api.post(COMPLETIONS).mock(return_value=httpx.Response(503))
    with pytest.raises(LlmUnavailableError):
        await _complete(rig, "dialogue", model="fb-model")
    assert route.call_count == 1


async def test_model_override_selects_the_model(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    route = llm_api.post(COMPLETIONS).mock(return_value=OK)
    assert (await _complete(rig, "router", model="custom-router")).model == "custom-router"
    assert _models(route) == ["custom-router"]


async def test_open_breaker_sends_dialogue_to_the_fallback_without_a_primary_request(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    breaker = rig.gateway.breaker("platform", "dlg-model")
    for _ in range(5):
        assert breaker.allow()
        breaker.record(success=False)
    route = llm_api.post(COMPLETIONS).mock(return_value=OK)
    completion = await _complete(rig, "dialogue")
    assert (completion.model, completion.attempts, completion.fell_back) == ("fb-model", 1, True)
    assert _models(route) == ["fb-model"]


async def test_open_breaker_fails_a_classifier_without_a_request(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    breaker = rig.gateway.breaker("platform", "rtr-model")
    for _ in range(5):
        assert breaker.allow()
        breaker.record(success=False)
    route = llm_api.post(COMPLETIONS).mock(return_value=OK)
    error = await _unavailable(rig, "router")
    assert (error.reason, error.last, route.call_count) == ("breaker_open", None, 0)


async def test_five_server_failures_open_the_breaker_and_it_recovers(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    route = llm_api.post(COMPLETIONS).mock(return_value=httpx.Response(500))
    for _ in range(2):
        await _unavailable(rig, "router")
    with pytest.raises(LlmUnavailableError) as caught:
        await _complete(rig, "router")
    assert caught.value.reason == "breaker_open"
    assert route.call_count == 5
    assert rig.gateway.breaker("platform", "rtr-model").state == "open"
    await rig.clock.run_for(20.0)
    route.mock(return_value=OK)
    assert (await _complete(rig, "router")).attempts == 1
    assert rig.gateway.breaker("platform", "rtr-model").state == "closed"


async def test_4xx_answers_keep_the_breaker_closed(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    llm_api.post(COMPLETIONS).mock(return_value=httpx.Response(429, headers={"retry-after": "9"}))
    for _ in range(6):
        await _unavailable(rig, "router")
    assert rig.gateway.breaker("platform", "rtr-model").state == "closed"


async def test_org_provider_failure_falls_back_to_the_platform_fallback_model(
    llm_api: respx.MockRouter,
) -> None:
    org_id = uuid.uuid4()
    async with gateway_rig(org_providers=StaticOrgProviders({org_id: org_provider(org_id)})) as rig:
        org_route = llm_api.post(ORG_COMPLETIONS).mock(return_value=httpx.Response(503))
        platform_route = llm_api.post(COMPLETIONS).mock(return_value=OK)
        completion = await rig.gateway.complete("dialogue", org_id=org_id, messages=[user()])
        assert (completion.provider_id, completion.model, completion.fell_back) == (
            "platform",
            "fb-model",
            True,
        )
        assert _models(org_route) == ["org-dlg-model"]
        assert org_route.calls.last.request.headers["authorization"] == "Bearer org-secret-key"
        assert platform_route.calls.last.request.headers["authorization"] == f"Bearer {PLATFORM_KEY}"


async def test_org_provider_serves_its_org_and_others_use_the_platform(llm_api: respx.MockRouter) -> None:
    org_id = uuid.uuid4()
    async with gateway_rig(org_providers=StaticOrgProviders({org_id: org_provider(org_id)})) as rig:
        org_route = llm_api.post(ORG_COMPLETIONS).mock(return_value=OK)
        platform_route = llm_api.post(COMPLETIONS).mock(return_value=OK)
        mine = await rig.gateway.complete("router", org_id=org_id, messages=[user()])
        other = await rig.gateway.complete("router", org_id=uuid.uuid4(), messages=[user()])
        assert (mine.provider_id, mine.model) == (f"org:{org_id}", "org-rtr-model")
        assert (other.provider_id, other.model) == ("platform", "rtr-model")
        assert (org_route.call_count, platform_route.call_count) == (1, 1)


async def test_org_classifier_failure_does_not_fall_back(llm_api: respx.MockRouter) -> None:
    org_id = uuid.uuid4()
    async with gateway_rig(org_providers=StaticOrgProviders({org_id: org_provider(org_id)})) as rig:
        llm_api.post(ORG_COMPLETIONS).mock(return_value=httpx.Response(503))
        platform_route = llm_api.post(COMPLETIONS).mock(return_value=OK)
        with pytest.raises(LlmUnavailableError):
            await rig.gateway.complete("router", org_id=org_id, messages=[user()])
        assert platform_route.call_count == 0


async def test_request_body_and_headers(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    route = llm_api.post(COMPLETIONS).mock(return_value=OK)
    call_id = uuid.uuid4()
    tool = ToolDef(name="select_path", description="Pick", parameters={"type": "object", "properties": {}})
    call = ToolCall(id="call_1", name="lookup", arguments_json='{"q": 1}')
    messages = [
        ChatMessage(role="system", content="Be brief."),
        ChatMessage(role="assistant", content=None, tool_calls=(call,)),
        ChatMessage(role="tool", content='{"ok": true}', tool_call_id="call_1"),
        user("Hi"),
    ]
    await rig.gateway.complete(
        "router",
        org_id=rig.org_id,
        messages=messages,
        tools=[tool],
        tool_choice=ForceTool("select_path"),
        temperature=0.2,
        max_tokens=64,
        call_id=call_id,
        response_format={"type": "json_object"},
    )
    request = route.calls.last.request
    assert request.headers["x-va-call-id"] == str(call_id)
    assert request.headers["authorization"] == f"Bearer {PLATFORM_KEY}"
    assert sent_json(request) == {
        "model": "rtr-model",
        "messages": [
            {"role": "system", "content": "Be brief."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": '{"q": 1}'},
                    }
                ],
            },
            {"role": "tool", "content": '{"ok": true}', "tool_call_id": "call_1"},
            {"role": "user", "content": "Hi"},
        ],
        "tools": [tool.wire()],
        "tool_choice": {"type": "function", "function": {"name": "select_path"}},
        "temperature": 0.2,
        "max_tokens": 64,
        "response_format": {"type": "json_object"},
    }


async def test_defaults_send_no_tools_and_no_call_id(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    route = llm_api.post(COMPLETIONS).mock(return_value=OK)
    await rig.gateway.complete("dialogue", org_id=rig.org_id, messages=[user()], tool_choice="auto")
    request = route.calls.last.request
    assert "x-va-call-id" not in request.headers
    assert sent_json(request) == {
        "model": "dlg-model",
        "messages": [{"role": "user", "content": "Hello there"}],
        "temperature": 0.0,
        "max_tokens": 512,
        "tool_choice": "auto",
    }


async def test_tool_calls_and_finish_reason_are_returned(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    calls = [
        {
            "id": "c1",
            "type": "function",
            "function": {"name": "select_path", "arguments": '{"path_id":"stay"}'},
        }
    ]
    llm_api.post(COMPLETIONS).mock(
        return_value=httpx.Response(
            200, json=completion_body(None, tool_calls=calls, finish_reason="tool_calls")
        )
    )
    completion = await _complete(rig, "router")
    assert completion.content is None
    assert completion.finish_reason == "tool_calls"
    assert completion.tool_calls == (
        ToolCall(id="c1", name="select_path", arguments_json='{"path_id":"stay"}'),
    )


async def test_concurrency_is_bounded_per_provider(llm_api: respx.MockRouter) -> None:
    async with gateway_rig(platform=make_provider(max_concurrency=1)) as rig:
        first = hanging()
        route = llm_api.post(COMPLETIONS).mock(side_effect=scripted(first, OK))
        one = asyncio.create_task(_complete(rig, "analysis"))
        await rig.clock.wait_for_sleep(60.0)
        two = asyncio.create_task(_complete(rig, "analysis"))
        for _ in range(20):
            await asyncio.sleep(0)
        assert (first.started, route.call_count) == (1, 0)
        first.release.set()
        await one
        await two
        assert route.call_count == 2


async def test_cancelling_a_call_frees_the_half_open_probe(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    breaker = rig.gateway.breaker("platform", "rtr-model")
    for _ in range(5):
        assert breaker.allow()
        breaker.record(success=False)
    await rig.clock.run_for(20.0)
    stuck = hanging()
    llm_api.post(COMPLETIONS).mock(side_effect=scripted(stuck, OK))
    task = asyncio.create_task(_complete(rig, "router"))
    await rig.clock.wait_for_sleep(1.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stuck.cancelled == 1
    assert _state(breaker) == "half_open"
    assert (await _complete(rig, "router")).attempts == 1
    assert _state(breaker) == "closed"
