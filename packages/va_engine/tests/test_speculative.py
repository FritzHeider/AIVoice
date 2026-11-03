"""Speculative dialogue generation (specs/11 §5.8; Decision P07-T08a rules 5-6)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from dialogue_helpers import FakeSource, counter, dialogue_rig
from handler_helpers import handler_ctx
from pydantic import JsonValue
from service_helpers import json_log_stream, log_events
from task_helpers import NOW, served, task_context

from va_common.time import FrozenClock
from va_domain.org import OrgSettings
from va_engine.dialogue import GatewayDialogue
from va_engine.handlers import TurnContext
from va_engine.models import TurnRecord
from va_engine.prompt import DialogueSettings, PathwayPrompt
from va_engine.speculative import Speculation, referenced_variables, speculation_allowed

BEFORE = [{"name": "first_name", "type": "string"}]


def _ctx(node_type: str = "Default", global_config: dict[str, Any] | None = None, **data: Any) -> TurnContext:
    raw = {"id": "n", "type": node_type, "data": {"name": "Order", **data}}
    return handler_ctx(raw, variables={"first_name": None, "size": "large"}, global_config=global_config)


def _allowed(node_type: str = "Default", *, enabled: bool = True, **data: Any) -> bool:
    global_config = data.pop("global_config", None)
    ctx = _ctx(node_type, global_config, **data)
    return speculation_allowed(ctx.node, ctx.graph, enabled=enabled)


async def _read(tokens: AsyncIterator[str]) -> str:
    return "".join([token async for token in tokens])


def test_referenced_variables_are_the_first_path_segments() -> None:
    assert referenced_variables(
        ["Hi {{ first_name | upper }} {{order.items[0].name}}", "{{request_data.x}}"]
    ) == frozenset({"first_name", "order", "request_data"})
    assert referenced_variables(["no expressions"]) == frozenset()
    assert referenced_variables(["fine {{a}}", "broken {{"]) is None


@pytest.mark.parametrize(
    ("node_type", "data", "enabled", "expected"),
    [
        ("Default", {"prompt": "Ask."}, True, True),
        ("Default", {"prompt": "Ask."}, False, False),
        ("Default", {"text": "Hello there."}, True, False),
        ("Scheduling", {}, True, True),
        ("Wait for Response", {}, True, False),
        ("Wait for Response", {"prompt": "Nudge."}, True, True),
        ("Webhook", {"prompt": "Tell the result."}, True, True),
        ("Webhook", {"prompt": "  "}, True, False),
        ("SMS", {"prompt": "Confirm."}, True, True),
        ("End Call", {"prompt": "Bye."}, True, False),
        ("Transfer Call", {"prompt": "Transferring."}, True, False),
        ("Press Button", {"prompt": "Press."}, True, False),
        ("Default", {"prompt": "Hi {{first_name}}", "extractVars": BEFORE}, True, False),
        ("Default", {"prompt": "Ask {{size}}", "extractVars": BEFORE}, True, True),
        (
            "Default",
            {"prompt": "Hi {{first_name}}", "extractVars": BEFORE, "extractVarsMode": "background"},
            True,
            True,
        ),
        ("Default", {"prompt": "Hi {{first_name", "extractVars": BEFORE}, True, False),
        (
            "Default",
            {"prompt": "Ask.", "extractVars": BEFORE, "global_config": {"globalPrompt": "Hi {{first_name}}"}},
            True,
            False,
        ),
        (
            "Default",
            {
                "prompt": "Ask.",
                "extractVars": BEFORE,
                "global_config": {"globalPrompt": "Hi {{first_name}}", "applyGlobalPromptToAllNodes": False},
            },
            True,
            True,
        ),
    ],
)
def test_speculation_eligibility(node_type: str, data: dict[str, Any], enabled: bool, expected: bool) -> None:
    assert _allowed(node_type, enabled=enabled, **data) is expected


async def _started(
    prompt: str = "Ask {{size}}.",
) -> tuple[Speculation, GatewayDialogue, FakeSource, TurnContext]:
    dialogue, source, _ = dialogue_rig(FakeSource().reply("Large ", "it is.").reply("Fresh reply."))
    ctx = _ctx(prompt=prompt)
    speculation = await Speculation.start(dialogue, ctx)
    return speculation, dialogue, source, ctx


async def test_committed_speculation_is_relayed_by_the_next_stream() -> None:
    speculation, dialogue, source, ctx = await _started()
    assert source.requests[0].speculative is True
    assert source.streams[0].yielded == []
    log = json_log_stream()
    ctx.state.variables["unrelated"] = "x"
    stream = await speculation.resolve(stays=True, variables=ctx.state.variables)
    assert stream is speculation.stream
    assert (speculation.outcome, speculation.reason) == ("committed", None)
    assert source.streams[0].committed is True
    assert await _read(dialogue.stream(ctx)) == "Large it is."
    assert len(source.streams) == 1
    assert [e["outcome"] for e in log_events(log) if e["event"] == "engine.speculation"] == ["committed"]


@pytest.mark.parametrize(
    ("stays", "prompt_changed", "change", "reason"),
    [
        (False, False, None, "route_changed"),
        (True, True, None, "prompt_changed"),
        (True, False, ("size", "small"), "variables_changed"),
        (True, False, ("size", None), "variables_changed"),
    ],
)
async def test_discarded_speculation_never_yields_and_a_fresh_request_runs(
    stays: bool, prompt_changed: bool, change: tuple[str, Any] | None, reason: str
) -> None:
    speculation, dialogue, source, ctx = await _started()
    variables = dict(ctx.state.variables)
    if change is not None:
        variables[change[0]] = change[1]
    log = json_log_stream()
    assert await speculation.resolve(stays=stays, variables=variables, prompt_changed=prompt_changed) is None
    assert (speculation.outcome, speculation.reason) == ("discarded", reason)
    first = source.streams[0]
    assert (first.discarded, first.closed, first.yielded) == (True, 1, [])
    assert await _read(dialogue.stream(ctx)) == "Fresh reply."
    assert source.requests[1].speculative is False
    [event] = [e for e in log_events(log) if e["event"] == "engine.speculation"]
    assert (event["outcome"], event["reason"], event["node_id"]) == ("discarded", reason, "n")


async def test_removed_referenced_variable_and_unparseable_prompt_compare_everything() -> None:
    speculation, _, _, ctx = await _started()
    variables = {key: value for key, value in ctx.state.variables.items() if key != "size"}
    assert await speculation.resolve(stays=True, variables=variables) is None
    broken, _, _, broken_ctx = await _started("Ask {{size")
    assert await broken.resolve(stays=True, variables={**broken_ctx.state.variables, "other": 1}) is None
    assert broken.reason == "variables_changed"
    same, _, _, same_ctx = await _started("Ask {{size")
    assert await same.resolve(stays=True, variables=dict(same_ctx.state.variables)) is not None


async def test_in_place_mutation_of_a_referenced_value_is_detected() -> None:
    dialogue, _, _ = dialogue_rig()
    ctx = _ctx(prompt="Order {{order}}")
    items: list[JsonValue] = ["pizza"]
    ctx.state.variables["order"] = {"items": items}
    speculation = await Speculation.start(dialogue, ctx)
    items.append("salad")
    assert await speculation.resolve(stays=True, variables=ctx.state.variables) is None
    assert speculation.reason == "variables_changed"


async def test_resolve_twice_and_aclose_after_resolution_do_nothing() -> None:
    speculation, _, source, ctx = await _started()
    stream = await speculation.resolve(stays=True, variables=ctx.state.variables)
    assert await speculation.resolve(stays=False, variables={}) is stream
    await speculation.aclose()
    assert (speculation.outcome, source.streams[0].discarded) == ("committed", False)
    discarded, _, _, _ = await _started()
    await discarded.resolve(stays=False, variables={})
    assert await discarded.resolve(stays=True, variables={}) is None
    assert discarded.outcome == "discarded"


async def test_aclose_discards_an_unresolved_speculation() -> None:
    speculation, _, source, _ = await _started()
    await speculation.aclose()
    assert (speculation.outcome, speculation.reason) == ("discarded", "closed")
    assert (source.streams[0].discarded, source.streams[0].closed) == (True, 1)


async def test_speculation_over_fakellm_commit_and_discard() -> None:
    async with served() as sim:
        await sim.rules(
            "rules:\n  - id: order\n    match: {last_user_regex: 'pizza'}\n"
            "    respond: {content: 'Which size would you like?'}\n"
        )
        settings = DialogueSettings.for_call(task_context(), OrgSettings())
        prompt = PathwayPrompt(settings, counter=counter(), clock=FrozenClock(NOW))
        dialogue = GatewayDialogue(sim.rig.gateway, prompt)
        ctx = _ctx(prompt="Take the order.")
        ctx.state.add_turn(TurnRecord(role="user", content="a pizza", node_id="n", at_ms=0))
        try:
            kept = await Speculation.start(dialogue, ctx)
            assert await kept.resolve(stays=True, variables=ctx.state.variables) is not None
            assert await _read(dialogue.stream(ctx)) == "Which size would you like?"
            dropped = await Speculation.start(dialogue, ctx)
            assert await dropped.resolve(stays=False, variables=ctx.state.variables) is None
            assert await _read(dropped.stream.text()) == ""
        finally:
            await dialogue.aclose()
        requests = await sim.requests()
        assert len(requests) >= 1
        assert sim.rig.metric("va_llm_speculation_total", outcome="committed") == 1
        assert sim.rig.metric("va_llm_speculation_total", outcome="discarded") == 1
