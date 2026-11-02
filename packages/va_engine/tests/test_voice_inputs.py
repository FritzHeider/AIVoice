"""Voice System Prompt inputs of a call (specs/10 §5, Decision P05-T09c)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from task_helpers import AGENT_NUMBER, CALLEE, NOW, task_context

from va_engine.voice_inputs import voice_prompt_inputs


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
