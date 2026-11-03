"""Pathway dialogue prompt assembly (specs/11 §5.7, §13; Decision P07-T08a rules 1-4)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from dialogue_helpers import CALL_ID, NOW, ORG_ID, counter, dialogue_rig, dialogue_settings
from graph_helpers import fixture
from handler_helpers import handler_ctx
from resolved_configs import resolved_config

from va_common.time import FrozenClock
from va_domain.call_context import CallContext
from va_domain.org import OrgSettings
from va_engine.handlers import TurnContext
from va_engine.models import EngineState, TurnRecord, load_graph
from va_engine.prompt import (
    DialogueSettings,
    PathwayPrompt,
    dialogue_model,
    dialogue_temperature,
    history_messages,
    instruction_templates,
    node_examples,
    node_instructions,
)
from va_llm.models import ChatMessage, ToolDef
from va_llm.prompts import EXAMPLES_INTRO, tool_usage_note
from va_llm.window import OMITTED_NOTE

GLOBAL = {"globalPrompt": "You are Ava from {{from}}."}


def _node(**data: Any) -> dict[str, Any]:
    return {"id": "n", "type": "Default", "data": {"name": "Take order", **data}}


def _record(role: str, content: str, **extra: Any) -> TurnRecord:
    return TurnRecord.model_validate({"role": role, "content": content, "node_id": "n", "at_ms": 0, **extra})


def _system(ctx: TurnContext, **settings: Any) -> str:
    _, _, prompt = dialogue_rig(**settings)
    return prompt.system(ctx)


# Rule 1: instructions.


def test_instructions_join_global_prompt_step_header_and_node_prompt() -> None:
    ctx = handler_ctx(_node(prompt="Ask {{first_name}} for the order."), variables={"first_name": "Mike"})
    ctx.graph.global_config.global_prompt = GLOBAL["globalPrompt"]
    assert node_instructions(ctx) == (
        "You are Ava from +14155550100.\n\n## Current step: Take order\nAsk Mike for the order."
    )


@pytest.mark.parametrize(
    ("apply_all", "prompt", "expected"),
    [
        (True, "Ask.", "G\n\n## Current step: Take order\nAsk."),
        (False, "Ask.", "## Current step: Take order\nAsk."),
        (False, None, "G\n\n## Current step: Take order"),
        (True, "   ", "G\n\n## Current step: Take order"),
    ],
)
def test_global_prompt_applies_to_all_nodes_or_to_nodes_without_a_prompt(
    apply_all: bool, prompt: str | None, expected: str
) -> None:
    config = {"globalPrompt": "G", "applyGlobalPromptToAllNodes": apply_all}
    ctx = handler_ctx(_node(prompt=prompt), global_config=config)
    assert node_instructions(ctx) == expected


def test_static_text_node_uses_its_text_and_a_blank_node_only_the_header() -> None:
    assert node_instructions(handler_ctx(_node(text="Hello {{from}}"))) == (
        "## Current step: Take order\nHello +14155550100"
    )
    assert node_instructions(handler_ctx(_node())) == "## Current step: Take order"


def test_blank_rendered_global_prompt_is_left_out_and_the_condition_suffix_comes_last() -> None:
    ctx = handler_ctx(_node(prompt="Ask."), global_config={"globalPrompt": "{{nothing}}"})
    suffix = 'The following condition is not yet satisfied; keep working toward it: "has order"'
    assert node_instructions(ctx, condition_suffix=f"  {suffix} ") == (
        f"## Current step: Take order\nAsk.\n\n{suffix}"
    )
    assert node_instructions(ctx, condition_suffix="   ") == "## Current step: Take order\nAsk."


def test_system_prompt_carries_runtime_facts_known_information_and_memory() -> None:
    ctx = handler_ctx(
        _node(prompt="Ask."),
        variables={"first_name": "Mike", "_secret": "s3", "account": "B-9"},
        timezone="America/New_York",
    )
    system = _system(ctx, memory_context="Likes pepperoni.")
    assert "Current date and time: 2026-11-03 12:45 (America/New_York), Tuesday." in system
    assert "## Current step: Take order\nAsk." in system
    assert "- first_name: Mike" in system
    assert "- account: B-9" in system  # variables win over request_data
    assert "A-7" not in system
    assert "_secret" not in system
    assert "Likes pepperoni." in system


def test_instruction_templates_are_the_applied_global_prompt_and_the_prompt_or_text() -> None:
    texted = handler_ctx(_node(text="Hi {{a}}"), global_config={"globalPrompt": "G {{b}}"})
    assert instruction_templates(texted.node, texted.graph) == ("G {{b}}", "Hi {{a}}")
    prompted = handler_ctx(_node(prompt="P", text="T"), global_config={"applyGlobalPromptToAllNodes": False})
    assert instruction_templates(prompted.node, prompted.graph) == ("P",)
    blank = handler_ctx(_node())
    assert instruction_templates(blank.node, blank.graph) == ()


# Rule 2: examples.


def test_dialogue_examples_pair_the_last_user_message_with_the_response() -> None:
    examples = [
        {
            "conversation": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "Hello!"},
                {"role": "user", "content": " a large pizza "},
            ],
            "assistantResponse": "One large pizza, got it.",
        },
        {"conversation": [{"role": "assistant", "content": "Hello"}], "assistantResponse": "x"},
        {"conversation": [{"role": "user", "content": "hm"}], "assistantResponse": "  "},
        {"conversation": [{"role": "user", "content": " "}], "assistantResponse": "y"},
    ]
    ctx = handler_ctx(_node(dialogueExamples=examples))
    assert node_examples(ctx.node) == (
        ChatMessage(role="system", content=EXAMPLES_INTRO),
        ChatMessage(role="user", content="a large pizza"),
        ChatMessage(role="assistant", content="One large pizza, got it."),
    )
    assert node_examples(handler_ctx(_node()).node) == ()


# Rule 3: history and window.


def test_history_maps_roles_and_turns_tool_and_system_records_into_context_updates() -> None:
    state = EngineState(runner="pathway", pathway_id=None, pathway_version=None, current_node_id="n")
    for record in (
        _record("assistant", "Hi, what can I get you?"),
        _record("user", "a pizza"),
        _record("assistant", "Sure, which—", interrupted=True),
        _record("tool", '{"price": 12}', tool_call_id="c1", tool_name="menu_lookup"),
        _record("tool", "ok", tool_call_id="c2"),
        _record("system", "Transfer failed."),
    ):
        state.add_turn(record)
    assert history_messages(state) == [
        ChatMessage(role="assistant", content="Hi, what can I get you?"),
        ChatMessage(role="user", content="a pizza"),
        ChatMessage(role="assistant", content="Sure, which—"),
        ChatMessage(role="system", content='[Context update] Tool menu_lookup returned: {"price": 12}'),
        ChatMessage(role="system", content="[Context update] Tool tool returned: ok"),
        ChatMessage(role="system", content="[Context update] Transfer failed."),
    ]


def test_request_puts_summary_before_the_window_and_trims_old_turns() -> None:
    ctx = handler_ctx(_node(prompt="Ask."))
    ctx.state.history_summary = "user: I called yesterday"
    filler = "word " * 400
    for index in range(6):
        ctx.state.add_turn(_record("user", f"{index} {filler}"))
        ctx.state.add_turn(_record("assistant", f"reply {index}"))
    dialogue, _, _ = dialogue_rig(context_budget_tokens=1_200)
    messages = dialogue.request(ctx).messages
    assert messages[1] == ChatMessage(
        role="system", content="[Context update] Earlier conversation: user: I called yesterday"
    )
    assert messages[2] == ChatMessage(role="system", content=OMITTED_NOTE)
    assert messages[-1] == ChatMessage(role="assistant", content="reply 5")
    assert messages[-2].content is not None
    assert messages[-2].content.startswith("5 ")
    assert len(messages) < 2 + 12


def test_request_appends_extra_messages_after_the_history() -> None:
    ctx = handler_ctx(_node(prompt="Ask."))
    ctx.state.add_turn(_record("user", "what's on the menu"))
    extra = [ChatMessage(role="system", content="[Context update] menu loaded")]
    dialogue, _, _ = dialogue_rig()
    dialogue.extra = extra
    assert dialogue.request(ctx).messages[-2:] == (
        ChatMessage(role="user", content="what's on the menu"),
        *extra,
    )


# Rule 4: model and temperature.


@pytest.mark.parametrize(
    ("node_model", "global_model", "call_model", "expected"),
    [
        ("node-m", "global-m", "call-m", "node-m"),
        (None, "global-m", "call-m", "global-m"),
        (None, None, "call-m", "call-m"),
        (None, None, None, None),
    ],
)
def test_dialogue_model_precedence(
    node_model: str | None, global_model: str | None, call_model: str | None, expected: str | None
) -> None:
    ctx = handler_ctx(
        _node(modelOptions={"model": node_model}), global_config={"dialogueModel": global_model}
    )
    assert dialogue_model(ctx, dialogue_settings(dialogue_model=call_model)) == expected


@pytest.mark.parametrize(
    ("node_value", "global_config", "call_value", "expected"),
    [
        (0.2, {"defaultTemperature": 0.9}, 0.4, 0.2),
        (None, {"defaultTemperature": 0.9}, 0.4, 0.9),
        (None, {"defaultTemperature": 0}, 0.4, 0.0),
        (None, {}, 0.4, 0.4),
        (None, {}, None, 0.7),
    ],
)
def test_dialogue_temperature_precedence(
    node_value: float | None, global_config: dict[str, Any], call_value: float | None, expected: float
) -> None:
    ctx = handler_ctx(_node(modelOptions={"temperature": node_value}), global_config=global_config)
    assert dialogue_temperature(ctx, dialogue_settings(temperature=call_value)) == expected


# The request.


def test_request_fields_and_tool_note() -> None:
    ctx = handler_ctx(_node(prompt="Ask."), turn_index=7)
    dialogue, _, _ = dialogue_rig(max_tokens=180)
    tool = ToolDef(name="lookup", description="Look up", parameters={"type": "object", "properties": {}})
    dialogue.tools = (tool,)
    dialogue.tool_speech = "Hang on."
    request = dialogue.request(ctx, speculative=True)
    assert (request.org_id, request.call_id, request.turn_index) == (ORG_ID, CALL_ID, 7)
    assert (request.model, request.temperature, request.max_tokens) == ("call-model", 0.4, 180)
    assert request.speculative is True
    assert request.tools == (tool,)
    assert request.messages[1] == ChatMessage(role="system", content=tool_usage_note("Hang on."))
    dialogue.tools = ()
    plain = dialogue.request(ctx)
    assert (plain.tools, plain.speculative) == ((), False)
    assert all(message.content != tool_usage_note("Hang on.") for message in plain.messages)


def test_restaurant_start_node_request_is_stable() -> None:
    raw = fixture("restaurant_ordering")
    loaded = load_graph(raw["nodes"], raw["edges"], raw["global_config"])
    start = loaded.start_nodes()[0]
    ctx = handler_ctx(_node())
    ctx.graph, ctx.node = loaded, start
    ctx.state.current_node_id = start.id
    ctx.state.add_turn(_record("user", "Hello?"))
    dialogue, _, _ = dialogue_rig()
    request = dialogue.request(ctx)
    system = request.messages[0].content
    assert system is not None
    assert system.split("Your instructions:\n", 1)[1] == (
        "You are Ava, a friendly receptionist for Bella Pasta. Keep answers short and natural.\n\n"
        "## Current step: Greeting\n"
        "Greet the caller warmly, say you are Ava from Bella Pasta, and ask how you can help.\n\n"
        "Known information:\n- account: A-7"
    )
    assert request.messages[1:] == (ChatMessage(role="user", content="Hello?"),)
    assert (request.model, request.temperature) == ("call-model", 0.6)
    assert dialogue.request(ctx).messages == request.messages


# Settings from a call.


def test_settings_for_call_map_the_context_and_org() -> None:
    config = {**resolved_config(), "dialogue_model": "m-1", "temperature": 0.3, "max_tokens": 120}
    context = CallContext.model_validate(
        {
            "call_id": uuid.uuid4(),
            "org_id": ORG_ID,
            "direction": "inbound",
            "to_number": "+14155550190",
            "from_number": "+14155550100",
            "config": config,
            "variables": {},
            "created_at": NOW,
        }
    )
    org = OrgSettings.model_validate(
        {"engine": {"speculative": False}, "llm": {"context_budget_tokens": 4000}}
    )
    settings = DialogueSettings.for_call(context, org, memory_context="m")
    assert settings == DialogueSettings(
        org_id=ORG_ID,
        direction="inbound",
        call_id=context.call_id,
        counterpart="+14155550190",
        language_name="English",
        dialogue_model="m-1",
        temperature=0.3,
        max_tokens=120,
        context_budget_tokens=4000,
        speculative=False,
        memory_context="m",
    )
    assert PathwayPrompt(settings, counter=counter(), clock=FrozenClock(NOW)).settings is settings
