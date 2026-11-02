"""TaskBrain against FakeLLM: opener, replies, tool loop, actions, barge-in (specs/11 §6, P05-T09c)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import orjson
import pytest
from llm_client_helpers import arm_fault
from llm_helpers import wait_held
from service_helpers import json_log_stream, log_events
from task_helpers import (
    AGENT_NUMBER,
    CALLEE,
    RecordingExecutor,
    Sim,
    assert_tool_pairing,
    drain,
    served,
    task_context,
    tool_messages,
    user_turn,
)

from va_domain.org import OrgSettings
from va_engine import task_runner
from va_engine.models.results import PressButton, TransferRequest
from va_engine.system_tools import SystemToolFacts
from va_engine.task_runner import AGENT_END_CALL, DEFAULT_GOODBYE, TASK_NODE_ID, TaskBrain
from va_engine.task_tools import INTERRUPTED, SKIPPED, TOO_MANY_TOOL_CALLS
from va_llm.dialogue import TOTAL_S
from va_llm.models import ToolDef
from va_llm.prompts import tool_usage_note
from va_llm.window import OMITTED_NOTE
from va_sim.llm.reply import GREETING
from va_voice.pipeline import BrainTiming, SpeakText

TASK = "You are Maya. Help the caller with their reservation."


@pytest.fixture
async def sim() -> AsyncIterator[Sim]:
    async with served() as running:
        yield running


def _tool_names(request: dict[str, Any]) -> list[str]:
    return [tool["function"]["name"] for tool in request["tools"] or []]


def _rule(rule_id: str, *, regex: str, respond: str, call_index: int | None = None) -> str:
    index = "" if call_index is None else f", call_index: {call_index}"
    match = f"{{last_user_regex: '{regex}'{index}}}"
    return f"rules:\n  - id: {rule_id}\n    match: {match}\n    respond: {respond}\n"


async def test_the_opener_is_the_templated_first_sentence(sim: Sim) -> None:
    context = task_context(
        first_sentence="Hi {{first_name}}, this is Maya calling from {{from}}.",
        request_data={"first_name": "Ana"},
    )
    brain = sim.brain(context)
    assert await drain(brain.start()) == [
        ("static", f"Hi Ana, this is Maya calling from {AGENT_NUMBER}.", True)
    ]
    [opener] = brain.messages
    assert (opener.role, opener.content) == (
        "assistant",
        f"Hi Ana, this is Maya calling from {AGENT_NUMBER}.",
    )
    [record] = brain.state.history
    assert (record.role, record.node_id, record.at_ms) == ("assistant", TASK_NODE_ID, 0)
    assert await sim.requests() == []


@pytest.mark.parametrize("first_sentence", [None, "   "])
async def test_without_a_first_sentence_the_model_writes_the_opener(
    sim: Sim, first_sentence: str | None
) -> None:
    brain = sim.brain(task_context(first_sentence=first_sentence))
    assert await drain(brain.start()) == [("text", GREETING)]
    [served_request] = await sim.requests()
    roles = [message["role"] for message in served_request["messages"]]
    assert roles == ["system", "system"]
    assert _tool_names(served_request) == ["end_call", "press_button"]
    assert brain.messages[0].content == GREETING


async def test_a_reply_streams_and_is_recorded(sim: Sim) -> None:
    context = task_context(task=TASK, temperature=0.3, max_tokens=120, request_data={"plan": "gold"})
    brain = sim.brain(context, memory_context="Prefers mornings.")
    await drain(brain.start())
    outputs = await drain(brain.on_user_turn(user_turn(1, "Can I move it to eight?")))
    assert outputs == [("text", "Okay. You said: Can I move it to eight?.")]
    assert [(m.role, m.content) for m in brain.messages] == [
        ("assistant", context.config.first_sentence),
        ("user", "Can I move it to eight?"),
        ("assistant", "Okay. You said: Can I move it to eight?."),
    ]
    assert brain.state.turn_index == 1
    assert [(r.role, r.at_ms) for r in brain.state.history][1] == ("user", 1600)
    [served_request] = await sim.requests()
    assert served_request["model"] == context.config.dialogue_model
    assert (served_request["temperature"], served_request["max_tokens"]) == (0.3, 120)
    assert served_request["call_id"] == str(context.call_id)
    system, note, *window = served_request["messages"]
    assert system["role"] == "system"
    assert TASK in system["content"]
    assert f"You placed this call to {CALLEE}." in system["content"]
    assert "Respond in English." in system["content"]
    assert "- plan: gold" in system["content"]
    assert "Prefers mornings." in system["content"]
    assert "(America/Chicago)" in system["content"]
    assert note == {"role": "system", "content": tool_usage_note()}
    assert [m["role"] for m in window] == ["assistant", "user"]
    assert _tool_names(served_request) == ["end_call", "press_button"]


async def test_instructions_are_templated_per_request(sim: Sim) -> None:
    brain = sim.brain(
        task_context(task="Greet {{name}}. The caller said: {{lastUserMessage}}", first_sentence="Hi.")
    )
    await drain(brain.start())
    await drain(brain.on_user_turn(user_turn(1, "hello there")))
    [served_request] = await sim.requests()
    assert "Greet . The caller said: hello there" in served_request["messages"][0]["content"]


async def test_an_invalid_template_falls_back_to_the_raw_text(sim: Sim) -> None:
    brain = sim.brain(task_context(task="Say {{ broken", first_sentence="Hello {{ also"))
    assert await drain(brain.start()) == [("static", "Hello {{ also", True)]
    await drain(brain.on_user_turn(user_turn(1, "hi")))
    [served_request] = await sim.requests()
    assert "Say {{ broken" in served_request["messages"][0]["content"]


async def test_a_missing_variable_is_logged_once(sim: Sim) -> None:
    stream = json_log_stream()
    brain = sim.brain(task_context(task="Help {{nobody}}.", first_sentence="Hi {{nobody}}."))
    await drain(brain.start())
    await drain(brain.on_user_turn(user_turn(1, "one")))
    await drain(brain.on_user_turn(user_turn(2, "two")))
    missing = [event for event in log_events(stream) if event["event"] == "template_variable_missing"]
    assert [event["path"] for event in missing] == ["nobody"]


async def test_end_call_with_a_final_message_ends_the_call(sim: Sim) -> None:
    await sim.rules(
        _rule(
            "bye",
            regex="bye",
            respond="{tool_calls: [{name: end_call, arguments: {reason: done, "
            "final_message: 'Thanks, bye now.'}}]}",
        )
    )
    brain = sim.brain(task_context())
    await drain(brain.start())
    outputs = await drain(brain.on_user_turn(user_turn(1, "ok bye")))
    assert outputs == [("end", "Thanks, bye now.", AGENT_END_CALL)]
    assert (brain.state.ended, brain.state.end_reason) == (True, "agent_end_call")
    assert [(m.role, m.content) for m in brain.messages[-2:]] == [
        ("tool", '{"status":"ending"}'),
        ("assistant", "Thanks, bye now."),
    ]
    assert len(await sim.requests()) == 1


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("Alright, take care.", [("text", "Alright, take care."), ("end", None, AGENT_END_CALL)]),
        (None, [("end", DEFAULT_GOODBYE, AGENT_END_CALL)]),
    ],
)
async def test_end_call_without_a_final_message(sim: Sim, content: str | None, expected: list[Any]) -> None:
    text = "" if content is None else f"content: '{content}', "
    await sim.rules(
        _rule(
            "bye",
            regex="bye",
            respond=f"{{{text}tool_calls: [{{name: end_call, arguments: {{reason: done}}}}]}}",
        )
    )
    brain = sim.brain(task_context())
    await drain(brain.start())
    assert await drain(brain.on_user_turn(user_turn(1, "bye"))) == expected


async def test_transfer_by_label_speaks_the_message_then_transfers(sim: Sim) -> None:
    await sim.rules(
        _rule(
            "t",
            regex="sales",
            respond="{tool_calls: [{name: transfer_call, arguments: {label: sales, "
            "message: 'Connecting you now.'}}, {name: press_button, arguments: {digits: '1'}}]}",
        )
    )
    context = task_context(transfer_list={"sales": "+14155550111", "support": "+14155550122"})
    brain = sim.brain(context)
    await drain(brain.start())
    outputs = await drain(brain.on_user_turn(user_turn(1, "sales please")))
    assert outputs == [
        ("static", "Connecting you now.", False),
        ("action", TransferRequest(target="+14155550111", label="sales", message="Connecting you now.")),
    ]
    [served_request] = await sim.requests()
    assert _tool_names(served_request) == ["end_call", "transfer_call", "press_button"]
    assert [(m.role, m.content) for m in brain.messages[-3:]] == [
        ("tool", '{"status":"transferring"}'),
        ("tool", SKIPPED),
        ("assistant", "Connecting you now."),
    ]


async def test_transfer_to_the_default_number(sim: Sim) -> None:
    await sim.rules(_rule("t", regex="human", respond="{tool_calls: [{name: transfer_call, arguments: {}}]}"))
    brain = sim.brain(task_context(transfer_phone_number="+14155550133"))
    await drain(brain.start())
    outputs = await drain(brain.on_user_turn(user_turn(1, "a human please")))
    assert outputs == [("action", TransferRequest(target="+14155550133", label=None, message=None))]
    assert TransferRequest(target="x").kind == "transfer_call"


async def test_a_failed_transfer_is_an_assistant_message_of_the_next_request(sim: Sim) -> None:
    await sim.rules(
        _rule(
            "t", regex="human", respond="{tool_calls: [{name: transfer_call, arguments: {}}]}", call_index=0
        )
    )
    brain = sim.brain(task_context(transfer_phone_number="+14155550133"))
    await drain(brain.start())
    await drain(brain.on_user_turn(user_turn(1, "a human please")))
    brain.on_transfer_failed("   ")
    brain.on_transfer_failed(" Sorry, nobody answered. ")
    await drain(brain.on_user_turn(user_turn(2, "okay then")))
    _, second = await sim.requests()
    tail = [(m["role"], m.get("content")) for m in second["messages"][-3:]]
    assert tail == [
        ("tool", '{"status":"transferring"}'),
        ("assistant", "Sorry, nobody answered."),
        ("user", "okay then"),
    ]


async def test_a_failed_transfer_message_cut_by_a_barge_in_keeps_what_was_heard(sim: Sim) -> None:
    brain = sim.brain(task_context(transfer_phone_number="+14155550133"))
    await drain(brain.start())
    brain.on_transfer_failed("Sorry, nobody answered. Anything else?")
    await brain.on_interrupted("Sorry, nobody—")
    assert (brain.messages[-1].role, brain.messages[-1].content) == ("assistant", "Sorry, nobody—")


async def test_an_unresolvable_transfer_label_is_answered_and_the_model_retries(sim: Sim) -> None:
    await sim.rules(
        _rule(
            "t",
            regex="billing",
            respond="{tool_calls: [{name: transfer_call, arguments: {label: billing}}]}",
            call_index=0,
        )
    )
    brain = sim.brain(task_context(transfer_phone_number="+14155550133"))
    await drain(brain.start())
    outputs = await drain(brain.on_user_turn(user_turn(1, "billing")))
    assert outputs == [("text", "Okay. You said: billing.")]
    first, second = await sim.requests()
    [answer] = tool_messages(second)
    assert answer["content"]["detail"] == [{"path": "label", "message": "unknown transfer label"}]
    assert_tool_pairing([first, second])


async def test_press_button_is_an_action_then_the_model_continues(sim: Sim) -> None:
    await sim.rules(
        _rule(
            "p",
            regex="menu",
            respond="{tool_calls: [{name: press_button, arguments: {digits: '12'}}]}",
            call_index=0,
        )
    )
    brain = sim.brain(task_context())
    await drain(brain.start())
    outputs = await drain(brain.on_user_turn(user_turn(1, "the menu")))
    assert outputs == [("action", PressButton(digits="12")), ("text", "Okay. You said: the menu.")]
    assert PressButton(digits="1").kind == "press_button"
    _, second = await sim.requests()
    [answer] = tool_messages(second)
    assert answer["content"] == {"status": "sent", "digits": "12"}
    assert [r.tool_name for r in brain.state.history if r.role == "tool"] == ["press_button"]


async def test_invalid_arguments_are_answered_with_the_errors(sim: Sim) -> None:
    await sim.rules(
        _rule(
            "p",
            regex="menu",
            respond="{tool_calls: [{name: press_button, arguments: {digits: '12x'}}]}",
            call_index=0,
        )
    )
    brain = sim.brain(task_context())
    await drain(brain.start())
    assert await drain(brain.on_user_turn(user_turn(1, "menu"))) == [("text", "Okay. You said: menu.")]
    _, second = await sim.requests()
    [answer] = tool_messages(second)
    assert answer["content"]["error"] == "invalid_arguments"
    assert answer["content"]["detail"][0]["path"] == "digits"


async def test_after_three_tool_rounds_the_model_is_asked_without_tools(sim: Sim) -> None:
    await sim.rules(
        _rule(
            "p",
            regex="menu",
            respond="{content: 'Pressing.', tool_calls: [{name: press_button, arguments: {digits: '1'}}]}",
        )
    )
    brain = sim.brain(task_context())
    await drain(brain.start())
    outputs = await drain(brain.on_user_turn(user_turn(1, "menu")))
    assert outputs == [("text", "Pressing."), ("action", PressButton(digits="1"))] * 3 + [
        ("text", "Pressing.")
    ]
    requests = await sim.requests()
    assert [bool(request["tools"]) for request in requests] == [True, True, True, False]
    assert requests[-1]["tool_choice"] is None
    assert_tool_pairing(requests)
    assert brain.messages[-1].tool_calls == ()


async def test_two_invalid_rounds_drop_the_tools(sim: Sim) -> None:
    await sim.rules(
        _rule("p", regex="menu", respond="{tool_calls: [{name: press_button, arguments: {digits: '!'}}]}")
    )
    brain = sim.brain(task_context())
    await drain(brain.start())
    assert await drain(brain.on_user_turn(user_turn(1, "menu"))) == []
    requests = await sim.requests()
    assert [bool(request["tools"]) for request in requests] == [True, True, False]
    assert_tool_pairing(requests)


async def _presses_on_menu(sim: Sim, brain: TaskBrain, *, count: int) -> list[Any]:
    """Opener, then a "menu" turn whose first reply presses digits ``1..count`` in one round."""
    calls = ", ".join(f"{{name: press_button, arguments: {{digits: '{d}'}}}}" for d in range(1, count + 1))
    await sim.rules(_rule("p", regex="menu", respond=f"{{tool_calls: [{calls}]}}", call_index=0))
    await drain(brain.start())
    return await drain(brain.on_user_turn(user_turn(1, "menu")))


async def test_calls_past_four_in_a_round_are_refused(sim: Sim) -> None:
    outputs = await _presses_on_menu(sim, sim.brain(task_context()), count=5)
    assert outputs[:4] == [("action", PressButton(digits=str(digit))) for digit in range(1, 5)]
    _, second = await sim.requests()
    assert tool_messages(second)[-1]["content"] == orjson.loads(TOO_MANY_TOOL_CALLS)


async def test_the_call_wide_tool_budget(sim: Sim, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(task_runner, "MAX_TOOL_CALLS_PER_CALL", 2)
    brain = sim.brain(task_context())
    outputs = await _presses_on_menu(sim, brain, count=3)
    assert outputs == [
        ("action", PressButton(digits="1")),
        ("action", PressButton(digits="2")),
        ("text", "Okay. You said: menu."),
    ]
    await drain(brain.on_user_turn(user_turn(2, "again")))
    requests = await sim.requests()
    assert [bool(request["tools"]) for request in requests] == [True, False, False]
    assert tool_messages(requests[1])[-1]["content"] == orjson.loads(TOO_MANY_TOOL_CALLS)


async def test_an_unavailable_llm_speaks_the_error_phrase(sim: Sim) -> None:
    context = task_context()
    brain = sim.brain(context)
    await drain(brain.start())
    await arm_fault(sim.control, kind="server_error", status=500, times=2)
    outputs = await drain(brain.on_user_turn(user_turn(1, "hello")))
    assert outputs == [("static", context.config.error_phrase, True)]
    assert brain.messages[-1].content == context.config.error_phrase
    assert len(await sim.requests()) == 2


async def test_a_failure_after_the_first_token_keeps_the_spoken_text(sim: Sim) -> None:
    await sim.rules(
        _rule(
            "p",
            regex="hours",
            respond="{content: 'We open at nine on weekdays.', tool_calls: [{name: press_button, "
            "arguments: {digits: '1'}}]}",
        )
    )
    brain = sim.brain(task_context())
    await drain(brain.start())
    await arm_fault(sim.control, kind="timeout", after_tokens=2)
    reply = asyncio.create_task(drain(brain.on_user_turn(user_turn(1, "hours?"))))
    await wait_held(sim.state, 1)
    await sim.rig.clock.wait_for_sleep(TOTAL_S)
    await sim.rig.clock.run_for(TOTAL_S)
    [(kind, text)] = await reply
    assert kind == "text"
    assert text
    assert "We open at nine on weekdays.".startswith(text)
    assert brain.messages[-1].tool_calls == ()
    assert len(await sim.requests()) == 1


async def test_a_keypad_turn_is_a_caller_pressed_message(sim: Sim) -> None:
    brain = sim.brain(task_context())
    await drain(brain.start())
    await drain(brain.on_user_turn(user_turn(1, "", dtmf="12")))
    [served_request] = await sim.requests()
    assert served_request["messages"][-1] == {"role": "user", "content": "[Caller pressed: 1 2]"}


async def test_a_superseding_turn_replaces_the_earlier_one(sim: Sim) -> None:
    brain = sim.brain(task_context())
    await drain(brain.start())
    first = brain.on_user_turn(user_turn(1, "I want"))
    output = await anext(first)
    assert isinstance(output, SpeakText)
    await first.aclose()  # the reply was cancelled before any audio
    await drain(brain.on_user_turn(user_turn(2, "I want a table", supersedes=1)))
    contents = [m.content for m in brain.messages]
    assert "I want" not in contents
    assert contents[-2:] == ["I want a table", "Okay. You said: I want a table."]
    assert [r.content for r in brain.state.history][-2:] == contents[-2:]
    assert len(brain.state.history) == 3
    await drain(brain.on_user_turn(user_turn(3, "more", supersedes=99)))
    assert brain.state.turn_index == 3


async def test_an_interrupted_reply_keeps_only_what_was_heard(sim: Sim) -> None:
    await sim.rules(
        _rule("long", regex="hours", respond="{content: 'We open at nine and close at ten every day.'}")
    )
    brain = sim.brain(task_context())
    await drain(brain.start())
    await drain(brain.on_user_turn(user_turn(1, "hours?")))
    await brain.on_interrupted("We open at nine—")
    await brain.on_interrupted("ignored, nothing is speaking")
    await drain(brain.on_user_turn(user_turn(2, "and weekends?")))
    _, second = await sim.requests()
    assistant = [m["content"] for m in second["messages"] if m["role"] == "assistant"]
    assert assistant[-1] == "We open at nine—"
    record = next(r for r in brain.state.history if r.content == "We open at nine—")
    assert record.interrupted


async def test_an_interrupted_opener_with_nothing_heard(sim: Sim) -> None:
    brain = sim.brain(task_context())
    await drain(brain.start())
    await brain.on_interrupted("")
    assert brain.messages[0].content == "—"
    assert brain.state.history[0].interrupted


async def test_closing_the_reply_while_streaming_keeps_the_partial_text(sim: Sim) -> None:
    await sim.rules(
        _rule("long", regex="hours", respond="{content: 'We open at nine and close at ten every day.'}")
    )
    brain = sim.brain(task_context())
    await drain(brain.start())
    outputs = brain.on_user_turn(user_turn(1, "hours?"))
    speech = await anext(outputs)
    assert isinstance(speech, SpeakText)
    tokens = aiter(speech.tokens)
    first = await anext(tokens)
    await tokens.aclose()  # type: ignore[attr-defined]
    await outputs.aclose()
    await brain.on_interrupted(first + "—")
    assert brain.messages[-1].content == first + "—"


async def test_closing_the_reply_mid_round_answers_the_pending_calls(sim: Sim) -> None:
    await sim.rules(
        _rule(
            "p",
            regex="menu",
            respond="{tool_calls: [{name: press_button, arguments: {digits: '1'}}, "
            "{name: press_button, arguments: {digits: '2'}}]}",
        )
    )
    brain = sim.brain(task_context())
    await drain(brain.start())
    outputs = brain.on_user_turn(user_turn(1, "menu"))
    while not isinstance(output := await anext(outputs), PressButton):
        if isinstance(output, SpeakText):
            assert [delta async for delta in output.tokens] == []
    await outputs.aclose()
    assert [(m.role, m.content) for m in brain.messages[-2:]] == [
        ("tool", '{"status":"sent","digits":"1"}'),
        ("tool", INTERRUPTED),
    ]
    await drain(brain.on_user_turn(user_turn(2, "next")))
    assert_tool_pairing(await sim.requests())


async def test_executor_tools_run_concurrently_and_answer_in_call_order(sim: Sim) -> None:
    lookup = ToolDef(
        name="lookup_order", parameters={"type": "object", "properties": {"id": {"type": "string"}}}
    )
    executor = RecordingExecutor(
        tools=(lookup,),
        speech="Let me check.",
        names=frozenset({"lookup_order", "send_sms"}),
        results={"send_sms": '{"status":"queued"}'},
        failing=frozenset({"explode"}),
        hold=True,
    )
    calls = ", ".join(f"{{name: lookup_order, arguments: {{id: '{n}'}}}}" for n in range(3))
    await sim.rules(
        _rule(
            "x",
            regex="orders",
            respond=f"{{tool_calls: [{calls}, {{name: send_sms, arguments: {{message: hi}}}}]}}",
            call_index=0,
        )
    )
    brain = sim.brain(task_context(), executor=executor, sms_capable=True)
    await drain(brain.start())
    reply = asyncio.create_task(drain(brain.on_user_turn(user_turn(1, "my orders"))))
    await executor.wait_active(3)
    for _ in range(20):
        await asyncio.sleep(0)
    assert (executor.active, len(executor.calls)) == (3, 3)
    executor.release.set()
    assert await reply == [("text", "Okay. You said: my orders.")]
    assert executor.max_active == 3
    first, second = await sim.requests()
    assert _tool_names(first) == ["end_call", "press_button", "send_sms", "lookup_order"]
    assert first["messages"][1]["content"] == tool_usage_note("Let me check.")
    answers = tool_messages(second)
    assert [a["content"] for a in answers] == [{"ok": "lookup_order"}] * 3 + [{"status": "queued"}]
    assert_tool_pairing([second])


async def test_a_failing_tool_and_an_unknown_tool_are_answered(sim: Sim) -> None:
    executor = RecordingExecutor(names=frozenset({"explode"}), failing=frozenset({"explode"}))
    await sim.rules(
        _rule("x", regex="go", respond="{tool_calls: [{name: explode}, {name: nonsense}]}", call_index=0)
    )
    brain = sim.brain(task_context(), executor=executor)
    await drain(brain.start())
    await drain(brain.on_user_turn(user_turn(1, "go")))
    _, second = await sim.requests()
    assert [a["content"] for a in tool_messages(second)] == [
        {"error": "tool_failed"},
        {"error": "invalid_arguments", "detail": "unknown_tool"},
    ]


async def test_without_an_executor_executor_backed_tools_are_not_offered(sim: Sim) -> None:
    facts = SystemToolFacts(
        runner="task", sms_capable=True, allow_schedule_callback=True, has_knowledge_bases=True
    )
    await sim.rules(
        _rule(
            "s",
            regex="text",
            respond="{tool_calls: [{name: send_sms, arguments: {message: hi}}]}",
            call_index=0,
        )
    )
    brain = sim.brain(task_context(), facts=facts)
    await drain(brain.start())
    await drain(brain.on_user_turn(user_turn(1, "text me")))
    first, second = await sim.requests()
    assert _tool_names(first) == ["end_call", "press_button"]
    assert tool_messages(second)[0]["content"]["detail"] == "unknown_tool"


async def test_a_long_conversation_is_windowed(sim: Sim) -> None:
    brain = sim.brain(
        task_context(), settings=OrgSettings.model_validate({"llm": {"context_budget_tokens": 1000}})
    )
    await drain(brain.start())
    for index in range(1, 25):
        await drain(brain.on_user_turn(user_turn(index, f"message {index} " + "words " * 20)))
    last = (await sim.requests())[-1]
    assert {"role": "system", "content": OMITTED_NOTE} in last["messages"]
    assert len(brain.messages) == 49


async def test_no_input_and_timing(sim: Sim) -> None:
    brain = sim.brain(task_context())
    assert await drain(brain.on_no_input()) == []
    assert brain.timing() == BrainTiming()


@pytest.mark.parametrize("config", [{"runner_type": "pathway"}, {"task": None}])
async def test_only_task_configs_are_accepted(sim: Sim, config: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="task-mode"):
        sim.brain(task_context(**config))


async def test_inbound_calls_name_the_dialled_number(sim: Sim) -> None:
    brain = sim.brain(task_context("inbound"))
    await drain(brain.start())
    await drain(brain.on_user_turn(user_turn(1, "hi")))
    [served_request] = await sim.requests()
    assert f"The caller called {AGENT_NUMBER}." in served_request["messages"][0]["content"]


async def test_a_greeting_comes_before_the_static_opener(sim: Sim) -> None:
    context = task_context(first_sentence="Hi, this is Maya.")
    brain = sim.brain(context)
    assert await drain(brain.start(user_turn(0, "Hello?"))) == [("static", "Hi, this is Maya.", True)]
    assert [(m.role, m.content) for m in brain.messages] == [
        ("user", "Hello?"),
        ("assistant", "Hi, this is Maya."),
    ]
    assert brain.state.turn_index == 1
    assert [r.role for r in brain.state.history] == ["user", "assistant"]
    assert await sim.requests() == []


async def test_a_generated_opener_answers_the_greeting(sim: Sim) -> None:
    brain = sim.brain(task_context(first_sentence=None))
    assert await drain(brain.start(user_turn(0, "Hello, who is this?"))) == [
        ("text", "Okay. You said: Hello, who is this?.")
    ]
    [served_request] = await sim.requests()
    assert served_request["messages"][-1] == {"role": "user", "content": "Hello, who is this?"}


async def test_a_keypad_greeting_is_a_caller_pressed_message(sim: Sim) -> None:
    brain = sim.brain(task_context(first_sentence=None))
    await drain(brain.start(user_turn(0, "", dtmf="9")))
    [served_request] = await sim.requests()
    assert served_request["messages"][-1] == {"role": "user", "content": "[Caller pressed: 9]"}
