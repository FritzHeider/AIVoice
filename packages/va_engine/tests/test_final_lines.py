"""Composed final lines over the LLM gateway (specs/11 §3.2.2-§3.2.3, Decision P07-T06 rule 3)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import pytest
from graph_helpers import node
from handler_helpers import FakeLines, handler_ctx, spoken
from task_helpers import served

from va_engine.final_lines import HISTORY_TURNS, LINE_MAX_TOKENS, GatewayLinePort
from va_engine.handlers import EndCallHandler, LineUnavailableError, TurnContext
from va_engine.models import TurnRecord
from va_llm.errors import LlmUnavailableError
from va_llm.models import ChatMessage, Completion, TokenUsage
from va_llm.policy import CallType

ORG = uuid.uuid4()
CALL = uuid.uuid4()


@dataclass
class RecordingGateway:
    """Answers ``content`` (or raises ``LlmUnavailableError`` when ``fail``) and records every request."""

    content: str | None = "Thanks for calling!"
    fail: bool = False
    requests: list[dict[str, Any]] = field(default_factory=list)

    async def complete(self, call_type: CallType, **options: Any) -> Completion:
        messages: Sequence[ChatMessage] = options["messages"]
        self.requests.append(
            {**options, "call_type": call_type, "messages": [(m.role, m.content) for m in messages]}
        )
        model = options.get("model")
        if self.fail:
            raise LlmUnavailableError("exhausted", provider_id="p", model="m", last=None)
        return Completion(
            content=self.content,
            tool_calls=(),
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1),
            provider_id="p",
            model=model if isinstance(model, str) else "m",
            attempts=1,
            fell_back=False,
        )


def end_ctx(global_config: dict[str, Any] | None = None, **data: Any) -> TurnContext:
    return handler_ctx(
        node("bye", node_type="End Call", **data),
        global_config=global_config,
        variables={"first_name": "Ann"},
    )


def add_history(ctx: TurnContext, count: int) -> None:
    for index in range(count):
        role: Literal["user", "assistant"] = "user" if index % 2 == 0 else "assistant"
        ctx.state.add_turn(TurnRecord(role=role, content=f"m{index}", node_id="bye", at_ms=index))
    ctx.state.add_turn(TurnRecord(role="tool", content="{}", node_id="bye", at_ms=count, tool_call_id="c1"))


async def test_request_carries_global_prompt_instruction_and_recent_history() -> None:
    gateway = RecordingGateway(content="  Bye, Ann!  ")
    lines = GatewayLinePort(gateway, org_id=ORG, call_id=CALL, model="platform-model")
    ctx = end_ctx({"globalPrompt": "You are Ava from {{account}}."})
    add_history(ctx, HISTORY_TURNS + 3)

    line = await lines.compose(ctx, "Say goodbye.")

    assert line == "Bye, Ann!"
    [request] = gateway.requests
    system = request["messages"][0]
    assert system == (
        "system",
        "You are Ava from A-7.\n\n## Your next line\nWrite only the line you say now: one or two short "
        "sentences, no markdown. Instruction: Say goodbye.",
    )
    history = request["messages"][1:]
    assert len(history) == HISTORY_TURNS
    assert history[-1] == ("user", f"m{HISTORY_TURNS + 2}")
    assert {role for role, _ in history} == {"user", "assistant"}
    assert request["call_type"] == "dialogue"
    assert (request["org_id"], request["call_id"]) == (ORG, CALL)
    assert request["max_tokens"] == LINE_MAX_TOKENS
    assert request["model"] == "platform-model"
    assert request["temperature"] == pytest.approx(0.7)


@pytest.mark.parametrize("global_prompt", [None, "   ", "{{nothing}}"])
async def test_without_a_global_prompt_only_the_instruction_is_sent(global_prompt: str | None) -> None:
    gateway = RecordingGateway()
    config = {"globalPrompt": global_prompt} if global_prompt is not None else None

    await GatewayLinePort(gateway, org_id=ORG).compose(end_ctx(config), "Wrap up.")

    [(role, content)] = gateway.requests[0]["messages"]
    assert role == "system"
    assert content.startswith("## Your next line")


@pytest.mark.parametrize(
    ("node_options", "global_config", "temperature", "model"),
    [
        (
            {"temperature": 0.2, "model": "node-model"},
            {"defaultTemperature": 0.9, "dialogueModel": "g"},
            0.2,
            "node-model",
        ),
        ({}, {"defaultTemperature": 0.9, "dialogueModel": "global-model"}, 0.9, "global-model"),
        ({"temperature": 0}, {}, 0.0, None),
    ],
)
async def test_temperature_and_model_precedence(
    node_options: dict[str, Any], global_config: dict[str, Any], temperature: float, model: str | None
) -> None:
    gateway = RecordingGateway()

    await GatewayLinePort(gateway, org_id=ORG).compose(
        end_ctx(global_config, modelOptions=node_options), "Go."
    )

    [request] = gateway.requests
    assert request["temperature"] == pytest.approx(temperature)
    assert request["model"] == model


@pytest.mark.parametrize(
    ("gateway", "reason"),
    [
        (RecordingGateway(fail=True), "llm_unavailable"),
        (RecordingGateway(content="   "), "blank"),
        (RecordingGateway(content=None), "blank"),
    ],
)
async def test_unusable_answers_raise_line_unavailable(gateway: RecordingGateway, reason: str) -> None:
    with pytest.raises(LineUnavailableError) as caught:
        await GatewayLinePort(gateway, org_id=ORG).compose(end_ctx(), "Go.")

    assert caught.value.reason == reason


async def test_end_call_prompt_mode_through_fake_llm() -> None:
    async with served() as sim:
        await sim.rules(
            "rules:\n  - id: bye\n    match: {last_user_regex: 'all set'}\n"
            "    respond: {content: 'Thanks for ordering, Ann. Goodbye!'}\n"
        )
        lines = GatewayLinePort(sim.rig.gateway, org_id=ORG, call_id=CALL)
        ctx = handler_ctx(
            node(
                "bye",
                node_type="End Call",
                endCall={
                    "speech": "Goodbye.",
                    "speechMode": "prompt",
                    "speechPrompt": "Thank {{first_name}}.",
                },
            ),
            variables={"first_name": "Ann"},
            lines=FakeLines(),
        )
        ctx.lines = lines
        ctx.state.add_turn(TurnRecord(role="user", content="That's all set.", node_id="bye", at_ms=1))

        result = await EndCallHandler().respond(ctx)

        assert await spoken(result.actions) == [
            ("say", "Thanks for ordering, Ann. Goodbye!", "t3.1", False),
            ("end", "agent_end_call", "agent", "t3.1"),
        ]
        [request] = await sim.requests()
        assert request["max_tokens"] == LINE_MAX_TOKENS
        assert request["messages"][0]["content"].endswith("Instruction: Thank Ann.")
