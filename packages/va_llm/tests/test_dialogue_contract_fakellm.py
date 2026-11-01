"""Streaming dialogue against FakeLLM on a real socket (specs/10 §2, §12, specs/30 §3.4; Decision P03-T13b).

The gateway runs on a manual clock (its TTFT and total deadlines fire when the test runs it); FakeLLM runs on
the wall clock with no latency.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import orjson
import pytest
from llm_client_helpers import GatewayRig, arm_fault, collect, dialogue, served_gateway, served_requests, user
from llm_helpers import add_rules, realtime_llm_state, wait_held

from va_llm import LlmConnectionError, LlmTimeoutError, ToolDef
from va_llm.dialogue import TOTAL_S, TTFT_S
from va_sim.settings import DEFAULT_LLM_MODELS
from va_sim.state import SimState

DIALOGUE, FALLBACK = DEFAULT_LLM_MODELS[0], DEFAULT_LLM_MODELS[2]
HOURS_RULE = (
    "rules:\n  - id: hours\n    match: {last_user_regex: 'open'}\n"
    "    respond: {content: 'We open at nine on weekdays.'}\n"
)


type Sim = tuple[GatewayRig, httpx.AsyncClient]


@pytest.fixture
def state() -> SimState:
    return realtime_llm_state()


@pytest.fixture
async def sim(state: SimState) -> AsyncIterator[Sim]:
    """The gateway and a control client of FakeLLM served on a socket."""
    async with served_gateway(state) as pair:
        yield pair


async def test_the_rule_reply_streams_token_by_token_with_usage(
    sim: Sim,
) -> None:
    rig, control = sim
    await add_rules(control, HOURS_RULE)
    async with await rig.gateway.stream_dialogue(dialogue(rig.org_id, user("When do you open?"))) as stream:
        deltas = await collect(stream)
    assert "".join(deltas) == "We open at nine on weekdays."
    assert len(deltas) > 1
    assert (stream.finish_reason, stream.model, stream.error) == ("stop", DIALOGUE, None)
    usage = stream.usage
    assert usage is not None
    assert usage.estimated is False
    assert usage.completion_tokens == len(deltas)
    [served] = await served_requests(control)
    assert (served["model"], served["stream"], served["rule_id"]) == (DIALOGUE, True, "hours")
    assert rig.usage.records[0].prompt_tokens == usage.prompt_tokens


async def test_a_tool_call_rule_is_assembled(sim: Sim) -> None:
    rig, control = sim
    await add_rules(
        control,
        "rules:\n  - id: bye\n    match: {last_user_regex: 'bye'}\n"
        "    respond: {tool_calls: [{name: end_call,\n"
        "      arguments: {reason: 'caller said goodbye', polite: true}}]}\n",
    )
    tool = ToolDef(name="end_call", description="End the call")
    async with await rig.gateway.stream_dialogue(
        dialogue(rig.org_id, user("ok bye"), tools=(tool,))
    ) as stream:
        assert await collect(stream) == []
        [call] = await stream.tool_calls()
    assert call.name == "end_call"
    assert call.id
    assert orjson.loads(call.arguments_json) == {"reason": "caller said goodbye", "polite": True}
    assert stream.finish_reason == "tool_calls"
    [served] = await served_requests(control)
    assert served["tool_choice"] == "auto"


@pytest.mark.parametrize("fault", [{"kind": "server_error", "status": 503}, {"kind": "disconnect"}])
async def test_a_failure_before_the_first_token_falls_back(sim: Sim, fault: dict[str, Any]) -> None:
    rig, control = sim
    await add_rules(control, HOURS_RULE)
    await arm_fault(control, **fault)
    async with await rig.gateway.stream_dialogue(dialogue(rig.org_id, user("Are you open?"))) as stream:
        assert "".join(await collect(stream)) == "We open at nine on weekdays."
    assert (stream.model, stream.fell_back, stream.attempts) == (FALLBACK, True, 2)
    assert [served["model"] for served in await served_requests(control)] == [DIALOGUE, FALLBACK]


async def test_a_held_request_hits_the_ttft_deadline(sim: Sim, state: SimState) -> None:
    rig, control = sim
    await arm_fault(control, kind="timeout")
    async with await rig.gateway.stream_dialogue(dialogue(rig.org_id)) as stream:
        reader = asyncio.create_task(collect(stream))
        await wait_held(state, 1)
        await rig.clock.wait_for_sleep(TTFT_S)
        await rig.clock.run_for(TTFT_S)
        assert await reader
    assert (stream.model, stream.fell_back) == (FALLBACK, True)
    await wait_held(state, 0)


async def test_a_stall_after_tokens_keeps_the_partial_text(sim: Sim, state: SimState) -> None:
    rig, control = sim
    await add_rules(control, HOURS_RULE)
    await arm_fault(control, kind="timeout", after_tokens=2)
    async with await rig.gateway.stream_dialogue(dialogue(rig.org_id, user("open?"))) as stream:
        reader = asyncio.create_task(collect(stream))
        await wait_held(state, 1)
        await rig.clock.wait_for_sleep(TOTAL_S)
        await rig.clock.run_for(TOTAL_S)
        deltas = await reader
    assert len(deltas) == 2
    assert "We open at nine on weekdays.".startswith("".join(deltas))
    assert isinstance(stream.error, LlmTimeoutError)
    assert stream.fell_back is False
    await wait_held(state, 0)
    usage = stream.usage
    assert usage is not None
    assert usage.estimated is True


async def test_discarding_a_speculative_stream_closes_the_request(sim: Sim, state: SimState) -> None:
    rig, control = sim
    await add_rules(control, HOURS_RULE)
    await arm_fault(control, kind="timeout", after_tokens=1)
    stream = await rig.gateway.stream_dialogue(dialogue(rig.org_id, user("open?"), speculative=True))
    await wait_held(state, 1)
    await rig.clock.wait_for_sleep(TOTAL_S)
    await stream.discard()
    await wait_held(state, 0)
    assert await collect(stream) == []
    assert stream.ttft_ms is not None
    assert not isinstance(stream.error, LlmConnectionError)
    assert rig.metric("va_llm_speculation_total", outcome="discarded") == 1
