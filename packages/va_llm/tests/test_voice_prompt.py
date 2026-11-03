"""Voice System Prompt rendering and §4 message helpers (specs/10 §4-§5 Decision P05-T09b)."""

from __future__ import annotations

import dataclasses
import locale
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from va_llm.models import ChatMessage
from va_llm.prompts import (
    DEFAULT_TOOL_SPEECH,
    VoicePromptInputs,
    assemble_messages,
    context_update,
    dtmf_message,
    example_messages,
    interrupted_message,
    load_prompt,
    render_answer_classifier,
    render_prompt,
    render_voice_system,
    tool_usage_note,
)

SPEC = Path(__file__).resolve().parents[3] / "specs" / "10-llm-gateway.md"
ZONE = "America/New_York"
NOW = datetime(2026, 11, 2, 15, 7, tzinfo=ZoneInfo(ZONE))
RULES_START = "\n\nHow to speak:\n"


def spec_block(heading: str) -> str:
    text = SPEC.read_text(encoding="utf-8")
    section = text[text.index(heading) :]
    match = re.search(r"```\n(.*?)\n```", section, flags=re.DOTALL)
    assert match is not None
    return match.group(1) + "\n"


def inputs(**overrides: Any) -> VoicePromptInputs:
    base = VoicePromptInputs(
        now_local=NOW,
        timezone=ZONE,
        direction="outbound",
        counterpart="+1 415-555-0100",
        language_name="English",
        instructions="Confirm the appointment.",
    )
    return dataclasses.replace(base, **overrides)


def rules() -> str:
    template = load_prompt("voice_system")
    start = template.index("How to speak:")
    end = template.index("\n\nYour instructions:")
    return template[start:end].replace("{language_name}", "English")


def test_prompt_file_is_the_spec_block_verbatim() -> None:
    assert load_prompt("voice_system") == spec_block("## 5. Voice System Prompt")


def test_minimal_outbound_prompt() -> None:
    assert render_voice_system(inputs()) == (
        "You are the assistant, speaking with a person on a live phone call.\n"
        "Current date and time: 2026-11-02 15:07 (America/New_York), Monday.\n"
        "You placed this call to +1 415-555-0100.\n"
        "\n"
        f"{rules()}\n"
        "\n"
        "Your instructions:\n"
        "Confirm the appointment."
    )


def test_full_inbound_prompt() -> None:
    prompt = render_voice_system(
        inputs(
            direction="inbound",
            counterpart="Acme Dental",
            agent_name="Ava",
            company="Acme Dental",
            instructions="\n  Book cleanings.\n\n",
            variables={"name": "Bob", "visits": 3},
            memory_context="Prefers mornings.",
            knowledge_chunks=["Cleanings take 45 minutes.", "We open at 8."],
            glossary_terms=["Invisalign"],
        )
    )

    assert prompt == (
        "You are Ava, speaking with a person on a live phone call on behalf of Acme Dental.\n"
        "Current date and time: 2026-11-02 15:07 (America/New_York), Monday.\n"
        "The caller called Acme Dental.\n"
        "\n"
        f"{rules()}\n"
        "\n"
        "Your instructions:\n"
        "Book cleanings.\n"
        "\n"
        "Known information:\n"
        "- name: Bob\n"
        "- visits: 3\n"
        "\n"
        "What you remember about this caller:\n"
        "Prefers mornings.\n"
        "\n"
        "Reference information (use it only if relevant; do not mention that you were given it):\n"
        "[1] Cleanings take 45 minutes.\n"
        "[2] We open at 8.\n"
        "\n"
        "Important names and terms: Invisalign"
    )


@pytest.mark.parametrize(
    ("direction", "line"),
    [
        ("outbound", "You placed this call to +1 415-555-0100."),
        ("inbound", "The caller called +1 415-555-0100."),
        ("web", "The person is talking to you through a website."),
    ],
)
def test_direction_line(direction: str, line: str) -> None:
    assert render_voice_system(inputs(direction=direction)).splitlines()[2] == line


@pytest.mark.parametrize(
    ("agent_name", "first"), [(None, "the assistant"), ("  ", "the assistant"), ("Max", "Max")]
)
def test_agent_name(agent_name: str | None, first: str) -> None:
    prompt = render_voice_system(inputs(agent_name=agent_name, company=" "))
    assert prompt.splitlines()[0] == f"You are {first}, speaking with a person on a live phone call."


def test_language_name_is_substituted() -> None:
    assert "- Respond in Spanish." in render_voice_system(inputs(language_name="Spanish"))


def test_known_information_rules() -> None:
    variables: dict[str, Any] = {
        "z": "last-in-name, first in order",
        "_secret": "hidden",
        "call_id": "c",
        "voice_id": "v",
        "flag": True,
        "nested": {"a": [1, None]},
        "price": 24.5,
    }

    prompt = render_voice_system(inputs(variables=variables))

    assert prompt.endswith(
        "Known information:\n"
        "- z: last-in-name, first in order\n"
        "- flag: true\n"
        '- nested: {"a":[1,null]}\n'
        "- price: 24.5"
    )


def test_known_information_omitted_when_only_excluded_keys() -> None:
    prompt = render_voice_system(inputs(variables={"_x": 1, "call_id": "c"}))
    assert "Known information" not in prompt


@pytest.mark.parametrize("memory", [None, "", "   "])
def test_memory_section_omitted_when_blank(memory: str | None) -> None:
    assert "What you remember" not in render_voice_system(inputs(memory_context=memory))


def test_glossary_deduplicates_case_insensitively() -> None:
    prompt = render_voice_system(inputs(glossary_terms=["Acme", "ACME", " Zed ", "", "acme"]))
    assert prompt.endswith("Important names and terms: Acme, Zed")
    assert "Important names" not in render_voice_system(inputs(glossary_terms=["  "]))


def test_braces_render_literally() -> None:
    prompt = render_voice_system(
        inputs(instructions="Say {agent_name} and {{x}}.", variables={"k": "{weekday}"}, company="{x}")
    )
    assert "Say {agent_name} and {{x}}." in prompt
    assert "- k: {weekday}" in prompt
    assert "on behalf of {x}." in prompt


@pytest.mark.parametrize(
    ("day", "name"),
    [
        (2, "Monday"),
        (3, "Tuesday"),
        (4, "Wednesday"),
        (5, "Thursday"),
        (6, "Friday"),
        (7, "Saturday"),
        (8, "Sunday"),
    ],
)
def test_weekday_is_english_whatever_the_locale(day: int, name: str) -> None:
    previous = locale.setlocale(locale.LC_TIME)
    try:
        for candidate in ("de_DE.UTF-8", "fr_FR.UTF-8", "C"):
            try:
                locale.setlocale(locale.LC_TIME, candidate)
                break
            except locale.Error:
                continue
        prompt = render_voice_system(inputs(now_local=NOW.replace(day=day)))
    finally:
        locale.setlocale(locale.LC_TIME, previous)
    assert prompt.splitlines()[1].endswith(f", {name}.")


def test_load_prompt_rejects_paths() -> None:
    with pytest.raises(ValueError, match="lower-case"):
        load_prompt("../voice_system")


def test_tool_usage_note() -> None:
    assert tool_usage_note() == (
        'You can use tools. Before using a tool that takes time, the caller will hear "One moment.". '
        "After a tool returns, tell the caller the result in plain spoken language."
    )
    assert DEFAULT_TOOL_SPEECH == "One moment."
    assert '"Let me check."' in tool_usage_note("Let me check.")


def test_spec_tool_note_sentence_matches() -> None:
    text = SPEC.read_text(encoding="utf-8")
    sentence = re.search(r"tool usage notes when tools exist: `(.*?)`", text)
    assert sentence is not None
    assert tool_usage_note("SPEECH") == sentence.group(1).replace("{speech}", "SPEECH")


def test_message_helpers() -> None:
    assert dtmf_message("123") == ChatMessage(role="user", content="[Caller pressed: 1 2 3]")
    assert context_update("New info") == ChatMessage(role="system", content="[Context update] New info")
    assert interrupted_message("Hello there ") == ChatMessage(role="assistant", content="Hello there—")
    assert example_messages([]) == ()
    assert example_messages([("Hi", "Hello!"), ("Price?", "Ten dollars.")]) == (
        ChatMessage(role="system", content="Examples of how to respond at this step:"),
        ChatMessage(role="user", content="Hi"),
        ChatMessage(role="assistant", content="Hello!"),
        ChatMessage(role="user", content="Price?"),
        ChatMessage(role="assistant", content="Ten dollars."),
    )


def test_assemble_messages_order() -> None:
    examples = example_messages([("Hi", "Hello!")])
    window = (ChatMessage(role="assistant", content="Hi there"), ChatMessage(role="user", content="Hey"))

    messages = assemble_messages("SYSTEM", tool_note="NOTE", examples=examples, window=window)

    assert messages == (
        ChatMessage(role="system", content="SYSTEM"),
        ChatMessage(role="system", content="NOTE"),
        *examples,
        *window,
    )
    assert assemble_messages("SYSTEM", tool_note=None) == (ChatMessage(role="system", content="SYSTEM"),)


# Voicemail / IVR classifier (specs/10 §9.4; specs/07 §14 Decision P05-T10c)


def test_answer_classifier_file_is_the_spec_block_verbatim() -> None:
    assert load_prompt("answer_classifier") == spec_block("### 9.4 Voicemail / IVR classifier")


def test_answer_classifier_render_fills_transcript_and_whole_seconds() -> None:
    rendered = render_answer_classifier('  You\'ve reached "Dana", leave a message.  ', 4.9)
    assert 'Transcript so far: "You\'ve reached "Dana", leave a message."' in rendered
    assert "Seconds since answer: 4\n" in rendered
    assert rendered.endswith("Call classify_answer exactly once.")
    assert "{" not in rendered


def test_answer_classifier_negative_seconds_render_as_zero() -> None:
    assert "Seconds since answer: 0\n" in render_answer_classifier("Hello?", -1.0)


# Pathway decision prompts (specs/10 §9.1-§9.3; specs/11 §5.5 Decision P07-T04)


@pytest.mark.parametrize(
    ("name", "heading"),
    [
        ("router", "### 9.1 Router"),
        ("loop_condition", "### 9.2 Loop condition"),
        ("extract", "### 9.3 Variable extraction"),
    ],
)
def test_decision_prompt_files_are_the_spec_blocks_verbatim(name: str, heading: str) -> None:
    assert load_prompt(name) == spec_block(heading)


def test_render_prompt_inserts_values_literally() -> None:
    rendered = render_prompt("loop_condition", {"condition": "Said {{name}} and {x}", "examples_block": ""})
    assert "Completion condition: Said {{name}} and {x}\n" in rendered
    assert rendered.endswith("say what is missing.\nCall score_condition exactly once.")


def test_render_prompt_needs_every_value() -> None:
    with pytest.raises(KeyError, match="examples_block"):
        render_prompt("loop_condition", {"condition": "c"})
