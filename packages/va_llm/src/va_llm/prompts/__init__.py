"""Voice dialogue prompts (specs/10-llm-gateway.md §4-§6; Decision 2026-11-02 P05-T09b).

:func:`render_voice_system` fills the verbatim Voice System Prompt (``voice_system.txt``) from plain values;
the engine maps a call's context onto :class:`VoicePromptInputs`. The dynamic data context lines follow the
rendered template as a ``## Context`` block (specs/10 §5 Decision P08-T03a). :func:`assemble_messages` puts
the §4 message list together, and :func:`fit_voice_system` keeps the system prompt within its share of the
model context.
"""

from __future__ import annotations

import dataclasses
import functools
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from importlib import resources
from typing import Literal

import orjson

from va_llm.models import ChatMessage
from va_llm.tokens import TokenCounter

__all__ = [
    "CONTEXT_HEADER",
    "DEFAULT_MAX_CONTEXT",
    "DEFAULT_TOOL_SPEECH",
    "MEMORY_MAX_TOKENS",
    "SYSTEM_SHARE",
    "VoicePromptInputs",
    "assemble_messages",
    "context_update",
    "dtmf_message",
    "example_messages",
    "fit_voice_system",
    "interrupted_message",
    "load_prompt",
    "render_answer_classifier",
    "render_prompt",
    "render_voice_system",
    "tool_usage_note",
]

DEFAULT_MAX_CONTEXT = 128_000
SYSTEM_SHARE = 0.6
MEMORY_MAX_TOKENS = 800
DEFAULT_TOOL_SPEECH = "One moment."
EXCLUDED_VARIABLES = frozenset({"call_id", "voice_id"})
EXAMPLES_INTRO = "Examples of how to respond at this step:"
INTERRUPTION_MARK = "—"
CONTEXT_HEADER = "## Context"

_PROMPT_NAME = re.compile(r"[a-z][a-z_]*")
_BAD_PROMPT_NAME = "prompt names are lower-case words joined by underscores"
_DEFAULT_AGENT_NAME = "the assistant"
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

type Direction = Literal["outbound", "inbound", "web"]
type PromptValue = str | int | float | bool | Sequence[PromptValue] | Mapping[str, PromptValue] | None


@dataclass(frozen=True, slots=True)
class VoicePromptInputs:
    """Everything the Voice System Prompt needs, as plain values.

    ``now_local`` is an aware datetime already in ``timezone``. ``counterpart`` is the outbound callee display
    or the inbound company/number (unused for web). ``knowledge_chunks`` are ranked best first.
    ``context_sections`` are the dynamic data context lines (specs/13 §7), in the order they are shown.
    """

    now_local: datetime
    timezone: str
    direction: Direction
    language_name: str
    instructions: str
    agent_name: str | None = None
    company: str | None = None
    counterpart: str = ""
    variables: Mapping[str, PromptValue] = field(default_factory=dict)
    memory_context: str | None = None
    knowledge_chunks: Sequence[str] = ()
    glossary_terms: Sequence[str] = ()
    context_sections: Sequence[str] = ()


def load_prompt(name: str) -> str:
    """The packaged prompt ``prompts/<name>.txt``."""
    if _PROMPT_NAME.fullmatch(name) is None:
        raise ValueError(_BAD_PROMPT_NAME)
    return _read_prompt(name)


@functools.cache
def _read_prompt(name: str) -> str:
    return resources.files("va_llm.prompts").joinpath(f"{name}.txt").read_text(encoding="utf-8")


def _direction_line(inputs: VoicePromptInputs) -> str:
    match inputs.direction:
        case "outbound":
            return f"You placed this call to {inputs.counterpart}."
        case "inbound":
            return f"The caller called {inputs.counterpart}."
        case "web":
            return "The person is talking to you through a website."


def _section(header: str, body: str) -> str:
    return f"\n{header}\n{body}\n"


def _value_text(value: PromptValue) -> str:
    return value if isinstance(value, str) else orjson.dumps(value).decode()


def _known_information(variables: Mapping[str, PromptValue]) -> str:
    lines = [
        f"- {name}: {_value_text(value)}"
        for name, value in variables.items()
        if not name.startswith("_") and name not in EXCLUDED_VARIABLES
    ]
    return _section("Known information:", "\n".join(lines)) if lines else ""


def _memory(memory_context: str | None) -> str:
    text = (memory_context or "").strip()
    return _section("What you remember about this caller:", text) if text else ""


def _knowledge(chunks: Sequence[str]) -> str:
    if not chunks:
        return ""
    lines = "\n".join(f"[{index}] {chunk}" for index, chunk in enumerate(chunks, start=1))
    return _section(
        "Reference information (use it only if relevant; do not mention that you were given it):", lines
    )


def _glossary(terms: Sequence[str]) -> str:
    seen: set[str] = set()
    unique: list[str] = []
    for term in terms:
        cleaned = term.strip()
        if cleaned and cleaned.casefold() not in seen:
            seen.add(cleaned.casefold())
            unique.append(cleaned)
    return f"\nImportant names and terms: {', '.join(unique)}\n" if unique else ""


def _context(sections: Sequence[str]) -> str:
    lines = [line for section in sections if (line := section.strip())]
    return f"\n\n{CONTEXT_HEADER}\n" + "\n".join(lines) if lines else ""


def render_voice_system(inputs: VoicePromptInputs) -> str:
    """The rendered Voice System Prompt (specs/10 §5), trailing whitespace removed, followed by the
    ``## Context`` block when there are context lines (blank ones are dropped; braces stay literal)."""
    agent_name = (inputs.agent_name or "").strip() or _DEFAULT_AGENT_NAME
    company = (inputs.company or "").strip()
    values = {
        "agent_name": agent_name,
        "company_clause": f" on behalf of {company}" if company else "",
        "now_local": inputs.now_local.strftime("%Y-%m-%d %H:%M"),
        "timezone": inputs.timezone,
        "weekday": _WEEKDAYS[inputs.now_local.weekday()],
        "direction_line": _direction_line(inputs),
        "language_name": inputs.language_name,
        "instructions": inputs.instructions.strip(),
        "known_information_section": _known_information(inputs.variables),
        "memory_section": _memory(inputs.memory_context),
        "knowledge_section": _knowledge(inputs.knowledge_chunks),
        "glossary_section": _glossary(inputs.glossary_terms),
    }
    return load_prompt("voice_system").format_map(values).rstrip() + _context(inputs.context_sections)


def render_answer_classifier(transcript: str, seconds: float) -> str:
    """The rendered voicemail / IVR classifier prompt (specs/10 §9.4): the callee's words so far (quotes
    left as they are) and the whole seconds since the answer; trailing whitespace removed."""
    values = {"transcript": transcript.strip(), "seconds": str(max(int(seconds), 0))}
    return load_prompt("answer_classifier").format_map(values).rstrip()


def render_prompt(name: str, values: Mapping[str, str]) -> str:
    """The packaged prompt ``name`` with every ``{field}`` filled from ``values`` (trailing whitespace
    removed).

    Values are inserted as they are (braces inside them stay literal); ``KeyError`` for a field that
    ``values`` lacks.
    """
    return load_prompt(name).format_map(values).rstrip()


def fit_voice_system(
    inputs: VoicePromptInputs,
    *,
    counter: TokenCounter,
    max_context: int = DEFAULT_MAX_CONTEXT,
    extra_tokens: int = 0,
) -> VoicePromptInputs:
    """Inputs whose prompt (plus ``extra_tokens``, e.g. examples) fits 60 % of ``max_context`` (specs/10 §6).

    Lowest-ranked knowledge chunks go first, then the memory context is cut to :data:`MEMORY_MAX_TOKENS`; the
    instructions and the context lines are never cut (the lines count towards the size), so the result may
    still be over the share.
    """
    limit = SYSTEM_SHARE * max_context

    def over(candidate: VoicePromptInputs) -> bool:
        return counter.text(render_voice_system(candidate)) + extra_tokens > limit

    fitted = inputs
    while fitted.knowledge_chunks and over(fitted):
        fitted = dataclasses.replace(fitted, knowledge_chunks=tuple(fitted.knowledge_chunks[:-1]))
    if fitted.memory_context and over(fitted):
        memory = counter.truncate(fitted.memory_context, MEMORY_MAX_TOKENS)
        fitted = dataclasses.replace(fitted, memory_context=memory)
    return fitted


def tool_usage_note(speech: str = DEFAULT_TOOL_SPEECH) -> str:
    """The §4 tool usage note; ``speech`` is the first custom tool's speech, else the first filler phrase."""
    return (
        f'You can use tools. Before using a tool that takes time, the caller will hear "{speech}". '
        "After a tool returns, tell the caller the result in plain spoken language."
    )


def dtmf_message(digits: str) -> ChatMessage:
    """A keypad turn: ``[Caller pressed: 1 2 3]``."""
    return ChatMessage(role="user", content=f"[Caller pressed: {' '.join(digits)}]")


def context_update(text: str) -> ChatMessage:
    """Context injected at its position in the conversation: ``[Context update] {text}``."""
    return ChatMessage(role="system", content=f"[Context update] {text}")


def interrupted_message(text_spoken: str) -> ChatMessage:
    """An assistant reply cut by the caller: only what was spoken, then ``—``."""
    return ChatMessage(role="assistant", content=text_spoken.rstrip() + INTERRUPTION_MARK)


def example_messages(pairs: Sequence[tuple[str, str]]) -> tuple[ChatMessage, ...]:
    """Pathway dialogue examples: the intro system message, then user/assistant pairs (``()`` for none)."""
    if not pairs:
        return ()
    messages = [ChatMessage(role="system", content=EXAMPLES_INTRO)]
    for user, assistant in pairs:
        messages.append(ChatMessage(role="user", content=user))
        messages.append(ChatMessage(role="assistant", content=assistant))
    return tuple(messages)


def assemble_messages(
    system: str,
    *,
    tool_note: str | None = None,
    examples: Sequence[ChatMessage] = (),
    window: Sequence[ChatMessage] = (),
) -> tuple[ChatMessage, ...]:
    """The §4 message list: system prompt, tool note, examples, then the conversation window."""
    head = [ChatMessage(role="system", content=system)]
    if tool_note:
        head.append(ChatMessage(role="system", content=tool_note))
    return (*head, *examples, *window)
