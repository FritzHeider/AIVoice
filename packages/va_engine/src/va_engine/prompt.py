"""Pathway dialogue prompt assembly (specs/11 §5.7, §13; Decision P07-T08a).

The pathway reply uses the specs/10 §4/§5 message layout of the task-mode runner: the Voice System Prompt
(its ``instructions`` = the applied global prompt, the ``## Current step`` block and the loop-condition
suffix), the tool note, the node's dialogue examples, the conversation summary, then the trimmed history
window (plus the caller's in-turn ``extra`` messages, e.g. tool rounds). The prompt's ``## Context`` block is
the call's pre-call dynamic data lines followed by the lines of its per-turn fetches, read from the
:class:`~va_engine.turn_data.TurnData` port at every request (specs/13 §7 Decision P08-T03b).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, cast

import orjson

from va_common.time import Clock
from va_domain.call_context import CallContext
from va_domain.languages import language_name
from va_domain.org import OrgSettings
from va_engine.handlers.base import TurnContext, has_content
from va_engine.models.graph import PathwayGraph
from va_engine.models.nodes import PathwayNode
from va_engine.models.state import EngineState, TurnRecord
from va_engine.templating import zone_named
from va_engine.turn_data import TurnData
from va_engine.voice_inputs import counterpart_of
from va_llm.dialogue import DialogueRequest
from va_llm.models import ChatMessage, ToolDef
from va_llm.prompts import (
    DEFAULT_TOOL_SPEECH,
    Direction,
    PromptValue,
    VoicePromptInputs,
    assemble_messages,
    context_update,
    example_messages,
    fit_voice_system,
    render_voice_system,
    tool_usage_note,
)
from va_llm.tokens import TokenCounter
from va_llm.window import ConversationWindow

__all__ = [
    "DEFAULT_TEMPERATURE",
    "SUMMARY_PREFIX",
    "DialogueSettings",
    "PathwayPrompt",
    "dialogue_model",
    "dialogue_temperature",
    "global_prompt_applies",
    "history_messages",
    "instruction_templates",
    "node_examples",
    "node_instructions",
]

DEFAULT_TEMPERATURE: Final = 0.7
SUMMARY_PREFIX: Final = "Earlier conversation: "
_CURRENT_STEP: Final = "## Current step: "


@dataclass(frozen=True, slots=True, kw_only=True)
class DialogueSettings:
    """The call- and org-level inputs of every pathway dialogue request.

    ``dialogue_model``/``temperature`` are the call config's values (the last layers of §13 before the
    platform default); ``speculative`` is the org's ``engine.speculative``; ``context_sections`` are the
    call's pre-call dynamic data context lines and ``turn_sections`` the lines its per-turn fetches had when
    the call started (the prompt's ``## Context`` block, specs/13 §7; a prompt with a
    :class:`~va_engine.turn_data.TurnData` port reads the turn lines from it instead).
    """

    org_id: uuid.UUID
    direction: Direction
    call_id: uuid.UUID | None = None
    counterpart: str = ""
    language_name: str = "English"
    dialogue_model: str | None = None
    temperature: float | None = None
    max_tokens: int = 250
    context_budget_tokens: int = 6_000
    speculative: bool = True
    memory_context: str | None = None
    context_sections: tuple[str, ...] = ()
    turn_sections: tuple[str, ...] = ()

    @classmethod
    def for_call(
        cls, context: CallContext, org: OrgSettings, *, memory_context: str | None = None
    ) -> DialogueSettings:
        """The settings of ``context`` under the org's ``settings``."""
        config = context.config
        return cls(
            org_id=context.org_id,
            direction=context.direction,
            call_id=context.call_id,
            counterpart=counterpart_of(context),
            language_name=language_name(config.language),
            dialogue_model=config.dialogue_model,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            context_budget_tokens=org.llm.context_budget_tokens,
            speculative=org.engine.speculative,
            memory_context=memory_context,
            context_sections=tuple(context.context_sections),
            turn_sections=tuple(context.turn_lines()),
        )


def global_prompt_applies(node: PathwayNode, graph: PathwayGraph) -> bool:
    """Whether the graph's non-blank ``globalPrompt`` joins ``node``'s instructions (§5.7 [1])."""
    config = graph.global_config
    if not has_content(config.global_prompt):
        return False
    return config.apply_global_prompt_to_all_nodes or not has_content(node.data.prompt)


def instruction_templates(node: PathwayNode, graph: PathwayGraph) -> tuple[str, ...]:
    """The template texts the instructions render: the applied global prompt and the node prompt (else its
    ``text``)."""
    texts: list[str] = []
    if global_prompt_applies(node, graph):
        texts.append(graph.global_config.global_prompt or "")
    if has_content(node.data.prompt):
        texts.append(node.data.prompt or "")
    elif has_content(node.data.text):
        texts.append(node.data.text or "")
    return tuple(texts)


def node_instructions(ctx: TurnContext, *, condition_suffix: str | None = None) -> str:
    """The Voice System Prompt ``instructions`` at the context's node (Decision P07-T08a rule 1)."""
    node, config = ctx.node, ctx.graph.global_config
    parts: list[str] = []
    if global_prompt_applies(node, ctx.graph):
        rendered = ctx.render(config.global_prompt or "", field_name="globalPrompt").strip()
        if rendered:
            parts.append(rendered)
    body = ""
    if has_content(node.data.prompt):
        body = ctx.render(node.data.prompt or "", field_name="prompt").strip()
    elif has_content(node.data.text):
        body = ctx.render(node.data.text or "", field_name="text").strip()
    header = _CURRENT_STEP + node.data.name
    parts.append(f"{header}\n{body}" if body else header)
    if condition_suffix and condition_suffix.strip():
        parts.append(condition_suffix.strip())
    return "\n\n".join(parts)


def node_examples(node: PathwayNode) -> tuple[ChatMessage, ...]:
    """The node's ``dialogueExamples`` as example messages (rule 2): each example's last user message paired
    with its ``assistantResponse``; examples without either are skipped."""
    pairs: list[tuple[str, str]] = []
    for example in node.data.dialogue_examples:
        users = [message.content for message in example.conversation if message.role == "user"]
        response = example.assistant_response.strip()
        if users and users[-1].strip() and response:
            pairs.append((users[-1].strip(), response))
    return example_messages(pairs)


def _history_message(record: TurnRecord) -> ChatMessage:
    if record.role == "tool":
        return context_update(f"Tool {record.tool_name or 'tool'} returned: {record.content}")
    if record.role == "system":
        return context_update(record.content)
    return ChatMessage(role=record.role, content=record.content)


def history_messages(state: EngineState) -> list[ChatMessage]:
    """``state.history`` as chat messages (rule 3); tool results and system notes become context updates
    (tool call ids and arguments are not persisted)."""
    return [_history_message(record) for record in state.history]


def _summary(state: EngineState) -> tuple[ChatMessage, ...]:
    summary = state.history_summary.strip()
    return (context_update(SUMMARY_PREFIX + summary),) if summary else ()


def dialogue_model(ctx: TurnContext, settings: DialogueSettings) -> str | None:
    """Node ``modelOptions.model`` → ``globalConfig.dialogueModel`` → the call's model (rule 4)."""
    node_model = ctx.node.data.model_options.model
    return node_model or ctx.graph.global_config.dialogue_model or settings.dialogue_model


def dialogue_temperature(ctx: TurnContext, settings: DialogueSettings) -> float:
    """Node ``modelOptions.temperature`` → an explicit ``defaultTemperature`` → the call's → 0.7 (rule 4)."""
    node_value = ctx.node.data.model_options.temperature
    if node_value is not None:
        return float(node_value)
    config = ctx.graph.global_config
    if "default_temperature" in config.model_fields_set:
        return float(config.default_temperature)
    if settings.temperature is not None:
        return settings.temperature
    return DEFAULT_TEMPERATURE


class PathwayPrompt:
    """Builds the :class:`DialogueRequest` of a pathway reply at the context's node."""

    def __init__(
        self,
        settings: DialogueSettings,
        *,
        counter: TokenCounter,
        clock: Clock,
        turn_data: TurnData | None = None,
    ) -> None:
        self._settings = settings
        self._counter = counter
        self._clock = clock
        self._turn_data = turn_data
        self._window = ConversationWindow(settings.context_budget_tokens, counter)

    @property
    def settings(self) -> DialogueSettings:
        return self._settings

    def system(self, ctx: TurnContext, *, condition_suffix: str | None = None, extra_tokens: int = 0) -> str:
        """The rendered Voice System Prompt at the context's node, fitted with ``extra_tokens`` beside it."""
        settings = self._settings
        variables = {**ctx.request_data, **ctx.state.variables}
        port = self._turn_data
        turn_sections = settings.turn_sections if port is None else port.sections()
        inputs = VoicePromptInputs(
            now_local=self._clock.now().astimezone(zone_named(ctx.timezone)),
            timezone=ctx.timezone,
            direction=settings.direction,
            language_name=settings.language_name,
            instructions=node_instructions(ctx, condition_suffix=condition_suffix),
            counterpart=settings.counterpart,
            variables=cast("dict[str, PromptValue]", variables),
            memory_context=settings.memory_context,
            context_sections=(*settings.context_sections, *turn_sections),
        )
        return render_voice_system(fit_voice_system(inputs, counter=self._counter, extra_tokens=extra_tokens))

    def request(  # noqa: PLR0913 - the request's per-turn knobs, all keyword-only
        self,
        ctx: TurnContext,
        *,
        condition_suffix: str | None = None,
        tools: tuple[ToolDef, ...] = (),
        tool_speech: str = DEFAULT_TOOL_SPEECH,
        extra: Sequence[ChatMessage] = (),
        speculative: bool = False,
    ) -> DialogueRequest:
        """The dialogue request at the context's node; ``extra`` follows the history inside the window."""
        settings = self._settings
        note = tool_usage_note(tool_speech) if tools else None
        examples = node_examples(ctx.node)
        summary = _summary(ctx.state)
        overhead = self._counter.messages((*examples, *summary))
        if note is not None:
            overhead += self._counter.text(note)
            overhead += self._counter.text(orjson.dumps([tool.wire() for tool in tools]).decode())
        system = self.system(ctx, condition_suffix=condition_suffix, extra_tokens=overhead)
        window = self._window.fit([*history_messages(ctx.state), *extra]).messages
        return DialogueRequest(
            org_id=settings.org_id,
            messages=assemble_messages(system, tool_note=note, examples=examples, window=(*summary, *window)),
            tools=tools,
            model=dialogue_model(ctx, settings),
            temperature=dialogue_temperature(ctx, settings),
            max_tokens=settings.max_tokens,
            speculative=speculative,
            call_id=settings.call_id,
            turn_index=ctx.state.turn_index,
        )
