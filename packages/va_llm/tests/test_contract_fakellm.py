"""Contract tests of the gateway against FakeLLM (specs/10 §12, specs/30 §3.4; Decision P03-T13a)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import httpx
import jsonschema
import orjson
import pytest
from llm_client_helpers import GatewayRig, arm_fault, served_requests, sim_gateway, user
from llm_helpers import add_rules, realtime_llm_state, wait_held
from llm_schemas import LOOP_SCHEMA

from va_llm import ForceTool, LlmTimeoutError, LlmUnavailableError, ToolDef, probe_provider
from va_sim.settings import DEFAULT_LLM_MODELS
from va_sim.state import SimState

DIALOGUE, FALLBACK = DEFAULT_LLM_MODELS[0], DEFAULT_LLM_MODELS[2]


@pytest.fixture
def state() -> SimState:
    return realtime_llm_state()


@pytest.fixture
async def sim(state: SimState) -> AsyncIterator[tuple[GatewayRig, httpx.AsyncClient]]:
    async with sim_gateway(state) as pair:
        yield pair


async def test_complete_returns_the_rule_reply_and_usage(sim: tuple[GatewayRig, httpx.AsyncClient]) -> None:
    rig, control = sim
    await add_rules(
        control,
        "rules:\n  - id: hours\n    match: {last_user_regex: 'open'}\n"
        "    respond: {content: 'We open at nine.'}\n",
    )
    completion = await rig.gateway.complete(
        "dialogue", org_id=rig.org_id, messages=[user("When do you open?")]
    )
    assert (completion.content, completion.finish_reason, completion.model) == (
        "We open at nine.",
        "stop",
        DIALOGUE,
    )
    assert completion.usage.estimated is False
    assert completion.usage.prompt_tokens > 0
    assert completion.usage.completion_tokens > 0
    [served] = await served_requests(control)
    assert (served["model"], served["stream"], served["rule_id"]) == (DIALOGUE, False, "hours")


async def test_call_id_header_selects_call_rules(sim: tuple[GatewayRig, httpx.AsyncClient]) -> None:
    rig, control = sim
    call_id = uuid.uuid4()
    await add_rules(
        control,
        f"rules:\n  - id: mine\n    match: {{call_id: '{call_id}'}}\n    respond: {{content: 'For you.'}}\n",
    )
    other_id = uuid.uuid4()
    mine = await rig.gateway.complete("dialogue", org_id=rig.org_id, messages=[user()], call_id=call_id)
    other = await rig.gateway.complete("dialogue", org_id=rig.org_id, messages=[user()], call_id=other_id)
    assert mine.content == "For you."
    assert other.content != "For you."
    assert [r["call_id"] for r in await served_requests(control)] == [str(call_id), str(other_id)]


async def test_a_server_error_is_retried(sim: tuple[GatewayRig, httpx.AsyncClient]) -> None:
    rig, control = sim
    await arm_fault(control, kind="server_error", status=503)
    completion = await rig.gateway.complete("router", org_id=rig.org_id, messages=[user()])
    assert completion.attempts == 2
    assert [r["status"] for r in await served_requests(control)] == [503, 200]


async def test_a_rate_limit_waits_retry_after_on_the_gateway_clock(
    sim: tuple[GatewayRig, httpx.AsyncClient],
) -> None:
    rig, control = sim
    await arm_fault(control, kind="rate_limit", retry_after_s=1)
    task = asyncio.create_task(rig.gateway.complete("extraction", org_id=rig.org_id, messages=[user()]))
    await rig.clock.wait_for_sleep(1.0)
    assert len(await served_requests(control)) == 1
    await rig.clock.run_for(1.0)
    assert (await task).attempts == 2


async def test_a_held_request_times_out_a_classifier(
    sim: tuple[GatewayRig, httpx.AsyncClient], state: SimState
) -> None:
    rig, control = sim
    await arm_fault(control, kind="timeout")
    task = asyncio.create_task(rig.gateway.complete("voicemail", org_id=rig.org_id, messages=[user()]))
    await wait_held(state, 1)
    await rig.clock.wait_for_sleep(1.5)
    await rig.clock.run_for(1.5)
    with pytest.raises(LlmUnavailableError) as caught:
        await task
    assert isinstance(caught.value.last, LlmTimeoutError)
    await wait_held(state, 0)


async def test_dialogue_falls_back_to_the_fallback_model(sim: tuple[GatewayRig, httpx.AsyncClient]) -> None:
    rig, control = sim
    await arm_fault(control, kind="server_error", status=502)
    completion = await rig.gateway.complete("dialogue", org_id=rig.org_id, messages=[user()])
    assert (completion.model, completion.fell_back) == (FALLBACK, True)
    assert [(r["model"], r["status"]) for r in await served_requests(control)] == [
        (DIALOGUE, 502),
        (FALLBACK, 200),
    ]


async def test_a_forced_tool_call_comes_back_schema_valid(sim: tuple[GatewayRig, httpx.AsyncClient]) -> None:
    rig, control = sim
    tool = ToolDef(name="score_condition", description="Score the condition", parameters=LOOP_SCHEMA)
    completion = await rig.gateway.complete(
        "loop_condition",
        org_id=rig.org_id,
        messages=[user("My name is Dana and my number is 555 0100.")],
        tools=[tool],
        tool_choice=ForceTool("score_condition"),
    )
    [call] = completion.tool_calls
    assert call.name == "score_condition"
    jsonschema.validate(orjson.loads(call.arguments_json), LOOP_SCHEMA)
    [served] = await served_requests(control)
    assert served["tool_choice"] == {"type": "function", "function": {"name": "score_condition"}}


async def test_probe_and_prewarm_against_fakellm(sim: tuple[GatewayRig, httpx.AsyncClient]) -> None:
    rig, control = sim
    assert await rig.gateway.prewarm()
    result = await probe_provider(rig.gateway.registry.platform, control)
    assert (result.supports_tool_choice, result.error) == (True, None)
    probe_request = (await served_requests(control))[0]
    assert (probe_request["max_tokens"], probe_request["model"]) == (16, DIALOGUE)
