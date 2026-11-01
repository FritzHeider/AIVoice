"""Usage, costs, and usage records (specs/10 §11; Decision P03-T13a rule 9)."""

from __future__ import annotations

import math
import uuid

import httpx
import pytest
import respx
from llm_client_helpers import COMPLETIONS, GatewayRig, completion_body, gateway_rig, user
from service_helpers import json_log_stream, log_events

from va_llm import MODEL_COSTS, ChatMessage, CostTable, ModelCost, TokenUsage, ToolCall, load_costs
from va_llm.metering import estimate_tokens, estimate_usage


def test_estimate_counts_utf8_bytes_over_4_plus_4_per_message() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2
    assert estimate_tokens("héllo") == 2  # 6 bytes
    call = ToolCall(id="c", name="lookup", arguments_json='{"q":"x"}')
    messages = [
        ChatMessage(role="system", content="Be brief."),  # 9 bytes -> 3
        ChatMessage(role="assistant", tool_calls=(call,)),  # lookup 2 + 9 bytes 3
        ChatMessage(role="user", content="Hi"),  # 1
    ]
    usage = estimate_usage(messages, "Hello there", (call,))
    assert usage == TokenUsage(prompt_tokens=4 + 3 + 4 + 5 + 4 + 1, completion_tokens=3 + 5, estimated=True)


def test_built_in_costs_cover_the_spec_models() -> None:
    assert set(MODEL_COSTS) == {
        "zai-org/GLM-5.3-Flash",
        "zai-org/GLM-5.3",
        "moonshotai/Kimi-K2.6",
        "Qwen/Qwen3-Embedding-8B",
    }


def test_costs_round_up_per_direction() -> None:
    table = CostTable({"m": ModelCost(in_per_1k_micros=50, out_per_1k_micros=200)})
    assert table.cost_micros("m", 1000, 1000) == (50, 200)
    assert table.cost_micros("m", 1, 1) == (1, 1)
    assert table.cost_micros("m", 0, 0) == (0, 0)
    assert table.cost_micros("m", 1234, 77) == (math.ceil(1234 * 50 / 1000), math.ceil(77 * 200 / 1000))


def test_unknown_models_cost_nothing_and_are_logged_once() -> None:
    stream = json_log_stream()
    table = CostTable({})
    assert table.cost_micros("mystery", 100, 100) == (0, 0)
    assert table.cost_micros("mystery", 5, 5) == (0, 0)
    events = [e for e in log_events(stream) if e["event"] == "llm.unpriced_model"]
    assert [e["model"] for e in events] == ["mystery"]


def test_json_overrides_merge_over_the_defaults() -> None:
    costs = load_costs(
        '{"zai-org/GLM-5.3": {"in_per_1k_micros": 1, "out_per_1k_micros": 2}, "new/m": '
        '{"in_per_1k_micros": 3, "out_per_1k_micros": 4}}'
    )
    assert costs["zai-org/GLM-5.3"] == ModelCost(1, 2)
    assert costs["new/m"] == ModelCost(3, 4)
    assert costs["moonshotai/Kimi-K2.6"] == MODEL_COSTS["moonshotai/Kimi-K2.6"]
    assert load_costs("") == MODEL_COSTS
    assert load_costs("{}") == MODEL_COSTS


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("{", "not valid JSON"),
        ("[]", "JSON object keyed by model"),
        ('{"m": 5}', "entry 'm' is invalid"),
        ('{"m": {"in_per_1k_micros": 1}}', "entry 'm' is invalid"),
        ('{"m": {"in_per_1k_micros": -1, "out_per_1k_micros": 1}}', "entry 'm' is invalid"),
        ('{"m": {"in_per_1k_micros": "1", "out_per_1k_micros": 1}}', "entry 'm' is invalid"),
        ('{"m": {"in_per_1k_micros": 1, "out_per_1k_micros": 1, "x": 1}}', "entry 'm' is invalid"),
    ],
)
def test_invalid_overrides_are_rejected(text: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        load_costs(text)


async def test_every_completion_is_recorded_with_usage_and_cost(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    llm_api.post(COMPLETIONS).mock(return_value=httpx.Response(200, json=completion_body(usage=(1000, 250))))
    call_id = uuid.uuid4()
    completion = await rig.gateway.complete("dialogue", org_id=rig.org_id, messages=[user()], call_id=call_id)
    assert completion.usage == TokenUsage(prompt_tokens=1000, completion_tokens=250)
    [record] = rig.usage.records
    assert (record.org_id, record.call_id, record.call_type) == (rig.org_id, call_id, "dialogue")
    assert (record.provider_id, record.model, record.estimated) == ("platform", "dlg-model", False)
    assert (record.prompt_tokens, record.completion_tokens) == (1000, 250)
    assert (record.cost_in_micros, record.cost_out_micros) == (100, 100)


async def test_fallback_completions_are_recorded_at_the_fallback_price(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    llm_api.post(COMPLETIONS).mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=completion_body(usage=(2000, 1000)))]
    )
    await rig.gateway.complete("dialogue", org_id=rig.org_id, messages=[user()])
    [record] = rig.usage.records
    assert (record.model, record.cost_in_micros, record.cost_out_micros, record.call_id) == (
        "fb-model",
        2000,
        3000,
        None,
    )


async def test_missing_usage_is_estimated(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    llm_api.post(COMPLETIONS).mock(
        return_value=httpx.Response(200, json=completion_body("Hello there", usage=None))
    )
    completion = await rig.gateway.complete("router", org_id=rig.org_id, messages=[user("Hi")])
    assert completion.usage == TokenUsage(prompt_tokens=5, completion_tokens=3, estimated=True)
    assert rig.usage.records[0].estimated


async def test_a_failing_recorder_is_logged_and_swallowed(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    stream = json_log_stream()
    llm_api.post(COMPLETIONS).mock(return_value=httpx.Response(200, json=completion_body()))
    rig.usage.failure = RuntimeError("billing down")
    completion = await rig.gateway.complete("router", org_id=rig.org_id, messages=[user()])
    assert completion.content == "Hello."
    [event] = [e for e in log_events(stream) if e["event"] == "llm.usage_record_failed"]
    assert (event["model"], event["error"]) == ("rtr-model", "RuntimeError")


async def test_failed_calls_record_nothing(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    llm_api.post(COMPLETIONS).mock(return_value=httpx.Response(400))
    with pytest.raises(Exception, match="unavailable"):
        await rig.gateway.complete("router", org_id=rig.org_id, messages=[user()])
    assert rig.usage.records == []


async def test_without_a_recorder_or_catalog_nothing_is_recorded(llm_api: respx.MockRouter) -> None:
    llm_api.post(COMPLETIONS).mock(return_value=httpx.Response(200, json=completion_body(usage=(4, 2))))
    async with gateway_rig(catalog=False, record_usage=False) as rig:
        completion = await rig.gateway.complete("dialogue", org_id=rig.org_id, messages=[user()])
        assert completion.usage == TokenUsage(prompt_tokens=4, completion_tokens=2)
        assert rig.usage.records == []
        assert (
            rig.registry.get_sample_value("va_llm_tokens_total", {"model": "dlg-model", "direction": "in"})
            is None
        )
