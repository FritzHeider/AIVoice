"""The opener as the phrase cache sees it, and TaskBrain speaking a pre-rendered opener (specs/07 §11 step 5,
Decision P05-T10d)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from service_helpers import json_log_stream, log_events
from task_helpers import AGENT_NUMBER, NOW, Sim, drain, served, task_context

from va_domain.voices import VoiceSettings
from va_engine.opener import cache_text, parse_field, phrase_format, phrase_voice, render_opener
from va_engine.templating import Template
from va_voice.audio.formats import PCM16LE_16000, ULAW_8000
from va_voice.phrase_cache import PhraseVoice

MODEL = "eleven_flash_v2_5"


@pytest.fixture
async def sim() -> AsyncIterator[Sim]:
    async with served() as running:
        yield running


def test_render_reads_variables_request_data_and_builtins() -> None:
    context = task_context(
        first_sentence="  Hi {{first_name}}, about {{plan}} on {{today}} from {{from}}.  ",
        request_data={"first_name": "Ana"},
        variables={"plan": "gold"},
        timezone="America/New_York",
    )
    assert render_opener(context, now=NOW) == f"Hi Ana, about gold on 2026-11-02 from {AGENT_NUMBER}."


def test_missing_variables_render_empty_and_are_logged_by_name() -> None:
    stream = json_log_stream()
    context = task_context(first_sentence="Hello {{nobody}}!")
    assert render_opener(context, now=NOW) == "Hello !"
    [event] = [e for e in log_events(stream) if e["event"] == "template_variable_missing"]
    assert (event["path"], event["call_id"]) == ("nobody", str(context.call_id))


@pytest.mark.parametrize("first_sentence", [None, "", "   ", "{{blank}}"])
def test_no_opener_without_text(first_sentence: str | None) -> None:
    context = task_context(first_sentence=first_sentence, request_data={"blank": "  "})
    assert render_opener(context, now=NOW) is None


def test_no_opener_for_other_runners() -> None:
    context = task_context(first_sentence="Hello.")
    other = context.model_copy(
        update={"config": context.config.model_copy(update={"runner_type": "pathway"})}
    )
    assert render_opener(other, now=NOW) is None


def test_a_template_syntax_error_speaks_the_raw_text() -> None:
    stream = json_log_stream()
    context = task_context(first_sentence="Hello {{name")
    assert render_opener(context, now=NOW) == "Hello {{name"
    [event] = [e for e in log_events(stream) if e["event"] == "template_invalid"]
    assert event["field"] == "first_sentence"


def test_parse_field_returns_a_template_or_the_text() -> None:
    assert isinstance(parse_field("Hi {{x}}", "task"), Template)
    assert parse_field("Hi {{x", "task") == "Hi {{x"


def test_phrase_voice_with_a_fixed_language() -> None:
    settings = VoiceSettings(stability=0.3)
    context = task_context(language="es", provider_voice_id="voice-9", voice_settings=settings.model_dump())
    assert phrase_voice(context, model_id=MODEL) == PhraseVoice("voice-9", MODEL, settings, "es")


def test_phrase_voice_with_detection_has_no_language() -> None:
    context = task_context(language="babel")
    assert phrase_voice(context, model_id=MODEL).language_code is None


@pytest.mark.parametrize(("hd_audio", "fmt"), [(False, ULAW_8000), (True, PCM16LE_16000)])
def test_phrase_format_follows_hd_audio(*, hd_audio: bool, fmt: object) -> None:
    assert phrase_format(task_context(hd_audio=hd_audio)) == fmt


def test_cache_text_applies_the_pronunciation_guide() -> None:
    context = task_context(pronunciation_guide=[{"word": "Acme", "pronunciation": "Ack-mee"}])
    assert cache_text(context, " Welcome to Acme. ") == "Welcome to Ack-mee."
    assert cache_text(task_context(), " Welcome. ") == "Welcome."


async def test_task_brain_speaks_the_prerendered_opener_verbatim(sim: Sim) -> None:
    context = task_context(first_sentence="Hi {{first_name}}.", request_data={"first_name": "Ana"})
    prerendered = context.model_copy(update={"opener_text": " Hi Ana, at 9:00 sharp. "})
    brain = sim.brain(prerendered)
    assert await drain(brain.start()) == [("static", "Hi Ana, at 9:00 sharp.", True)]
    assert brain.messages[0].content == "Hi Ana, at 9:00 sharp."
    assert await sim.requests() == []


@pytest.mark.parametrize("opener_text", [None, "  "])
async def test_task_brain_renders_the_template_without_a_prerender(sim: Sim, opener_text: str | None) -> None:
    context = task_context(first_sentence="Hi {{first_name}}.", request_data={"first_name": "Ana"})
    brain = sim.brain(context.model_copy(update={"opener_text": opener_text}))
    assert await drain(brain.start()) == [("static", "Hi Ana.", True)]
