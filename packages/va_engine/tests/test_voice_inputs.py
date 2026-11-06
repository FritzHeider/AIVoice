"""Voice System Prompt inputs of a call (specs/10 §5, Decision P05-T09c)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from dialogue_helpers import FakeSource, counter
from task_helpers import AGENT_NUMBER, CALLEE, NOW, drain, task_context, user_turn

from va_common.time import FrozenClock
from va_domain.org import OrgSettings
from va_engine.system_tools import facts_for_call
from va_engine.task_runner import TaskBrain
from va_engine.voice_inputs import voice_prompt_inputs
from va_llm.prompts import render_voice_system

LINES = ["The customer's name is Ana.", "Their plan is Pro."]


@pytest.mark.parametrize(
    ("direction", "counterpart"), [("outbound", CALLEE), ("inbound", AGENT_NUMBER), ("web", "")]
)
def test_direction_and_counterpart(direction: str, counterpart: str) -> None:
    inputs = voice_prompt_inputs(task_context(direction), now=NOW, instructions="Help.", variables={})
    assert (inputs.direction, inputs.counterpart) == (direction, counterpart)


def test_language_zone_and_values() -> None:
    context = task_context(language="es-MX", timezone="America/New_York")
    inputs = voice_prompt_inputs(
        context, now=NOW, instructions="Ayuda.", variables={"plan": "gold"}, memory_context="Likes tea."
    )
    assert inputs.language_name == "Spanish"
    assert inputs.timezone == "America/New_York"
    assert inputs.now_local.utcoffset() == timedelta(hours=-5)
    assert inputs.now_local == NOW
    assert (inputs.instructions, inputs.memory_context) == ("Ayuda.", "Likes tea.")
    assert dict(inputs.variables) == {"plan": "gold"}
    assert (inputs.agent_name, inputs.company, tuple(inputs.knowledge_chunks)) == (None, None, ())


def test_detection_languages_have_no_fixed_name() -> None:
    inputs = voice_prompt_inputs(task_context(language="auto"), now=NOW, instructions="", variables={})
    assert inputs.language_name == "the caller's language"


def test_the_contexts_dynamic_data_lines_become_the_context_block() -> None:
    context = task_context().model_copy(update={"context_sections": LINES})

    inputs = voice_prompt_inputs(context, now=NOW, instructions="Help.", variables={})

    assert tuple(inputs.context_sections) == tuple(LINES)
    assert render_voice_system(inputs).endswith("\n\n## Context\n" + "\n".join(LINES))
    plain = voice_prompt_inputs(task_context(), now=NOW, instructions="Help.", variables={})
    assert tuple(plain.context_sections) == ()


def test_the_turn_lines_follow_the_pre_call_lines() -> None:
    context = task_context().model_copy(
        update={"context_sections": LINES, "turn_sections": {3: ["Seats: 4."], 1: ["Balance: 12."]}}
    )

    stored = voice_prompt_inputs(context, now=NOW, instructions="Help.", variables={})
    refreshed = voice_prompt_inputs(
        context, now=NOW, instructions="Help.", variables={}, turn_sections=("Balance: 0.",)
    )
    none = voice_prompt_inputs(context, now=NOW, instructions="Help.", variables={}, turn_sections=())

    assert tuple(stored.context_sections) == (*LINES, "Balance: 12.", "Seats: 4.")
    assert tuple(refreshed.context_sections) == (*LINES, "Balance: 0.")
    assert tuple(none.context_sections) == tuple(LINES)


async def test_a_task_call_sends_pre_call_variables_and_context_to_the_model() -> None:
    context = task_context(
        variables={"customer_name": "Ana", "plan": "Pro"},
        task="Greet {{customer_name}} and confirm the {{plan}} plan.",
        first_sentence="Hi {{customer_name}}.",
    ).model_copy(update={"context_sections": LINES})
    settings = OrgSettings()
    source = FakeSource().reply("Sure.")
    brain = TaskBrain(
        context=context,
        settings=settings,
        gateway=source,
        counter=counter(),
        facts=facts_for_call(context, settings, sms_capable=False),
        clock=FrozenClock(NOW),
    )

    opener = await drain(brain.start())
    await drain(brain.on_user_turn(user_turn(1, "hello")))

    assert opener[0][:2] == ("static", "Hi Ana.")
    system = source.requests[0].messages[0].content
    assert system is not None
    assert "Greet Ana and confirm the Pro plan." in system
    assert "Known information:\n- customer_name: Ana\n- plan: Pro" in system
    assert system.endswith("\n\n## Context\nThe customer's name is Ana.\nTheir plan is Pro.")
