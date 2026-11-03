"""What every pathway node handler shares (specs/11 §4, Decision P07-T06).

A handler turns the current node into :data:`~va_engine.models.results.Action`\\ s:
:meth:`NodeHandler.on_enter` when the node becomes current, :meth:`NodeHandler.respond` for the agent's reply.
Both return a :class:`HandlerResult`; a broken node config never raises, the result carries a ``problem``
instead.

Side effects stay outside: the dialogue reply streams from the injected :class:`DialoguePort` (the turn
orchestration assembles the §5.7 prompt), composed final lines come from the :class:`LinePort`. Templates
render against the state's variables, ``request_data`` and the built-ins; a template with a syntax error is
used as raw text (``engine.template_invalid``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Final, Literal, Protocol

from va_common.logging import get_logger
from va_engine.models.graph import PathwayGraph
from va_engine.models.nodes import PathwayNode
from va_engine.models.results import Action, Say, SayStream, is_terminal
from va_engine.models.state import EngineState
from va_engine.templating import MissingVariables, TemplateScope, TemplateSyntaxError, render

__all__ = [
    "TEXT_CHANNELS",
    "Channel",
    "DialoguePort",
    "HandlerResult",
    "LinePort",
    "LineUnavailableError",
    "MarkSequence",
    "NodeHandler",
    "TurnContext",
    "final_line",
    "has_content",
    "interruptible_of",
    "is_text",
    "line_actions",
    "speak_node",
]

type Channel = Literal["voice", "sms", "widget_chat", "widget_voice"]

TEXT_CHANNELS: Final[frozenset[str]] = frozenset({"sms", "widget_chat"})

_log = get_logger(__name__)


def is_text(channel: Channel) -> bool:
    """Whether ``channel`` exchanges text messages (SMS, widget chat) rather than audio."""
    return channel in TEXT_CHANNELS


class LineUnavailableError(Exception):
    """A final line could not be composed; ``reason`` says why (``llm_unavailable``, ``blank``)."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"final line unavailable: {reason}")
        self.reason = reason


class DialoguePort(Protocol):
    """Streams the dialogue model's reply at the context's node (prompt assembly specs/11 §5.7)."""

    def stream(self, ctx: TurnContext) -> AsyncIterator[str]: ...


class LinePort(Protocol):
    """Composes one final line (End Call / Transfer Call ``speechPrompt``) following ``instruction``.

    Raises :class:`LineUnavailableError` when no line can be produced.
    """

    async def compose(self, ctx: TurnContext, instruction: str) -> str: ...


@dataclass(slots=True)
class MarkSequence:
    """Playback mark names of one conversation turn: ``t{turn_index}.{n}`` with ``n`` counting from 1."""

    issued: int = 0

    def next(self, turn_index: int) -> str:
        self.issued += 1
        return f"t{turn_index}.{self.issued}"


@dataclass(slots=True, kw_only=True)
class TurnContext:
    """The conversation as a handler sees it during one turn.

    ``marks`` is shared by every handler call of the turn (pass the same object when the node changes), so
    mark names stay unique within a turn.
    """

    state: EngineState
    graph: PathwayGraph
    node: PathwayNode
    channel: Channel
    dialogue: DialoguePort
    lines: LinePort
    now_ms: Callable[[], int]
    request_data: Mapping[str, object] = field(default_factory=dict)
    builtins: Mapping[str, object] = field(default_factory=dict)
    timezone: str = "UTC"
    missing: MissingVariables | None = None
    transfer_list: Mapping[str, str] = field(default_factory=dict)
    marks: MarkSequence = field(default_factory=MarkSequence)

    def scope(self) -> TemplateScope:
        """The template scope over the current variables."""
        return TemplateScope(
            variables=self.state.variables,
            request_data=self.request_data,
            builtins=self.builtins,
            timezone=self.timezone,
            missing=self.missing,
        )

    def render(self, text: str, *, field_name: str) -> str:
        """``text`` rendered; a template syntax error is logged and the raw text is returned."""
        try:
            return render(text, self.scope())
        except TemplateSyntaxError as exc:
            _log.warning(
                "engine.template_invalid", node_id=self.node.id, field=field_name, position=exc.position
            )
            return text

    def next_mark(self) -> str:
        """A fresh playback mark name for this turn."""
        return self.marks.next(self.state.turn_index)


@dataclass(frozen=True, slots=True)
class HandlerResult:
    """The actions a handler produced (executed in order) and the config problem it hit, if any."""

    actions: tuple[Action, ...] = ()
    problem: str | None = None

    @property
    def terminal(self) -> bool:
        """Whether an action ends the AI's part of the conversation (transfer, end call, human takeover)."""
        return any(is_terminal(action) for action in self.actions)


class NodeHandler(Protocol):
    """One node type's runtime semantics (specs/11 §4)."""

    async def on_enter(self, ctx: TurnContext) -> HandlerResult: ...

    async def respond(self, ctx: TurnContext) -> HandlerResult: ...

    def waits_for_user(self, node: PathwayNode) -> bool: ...


def has_content(text: str | None) -> bool:
    """Whether ``text`` is present and not blank."""
    return text is not None and bool(text.strip())


def interruptible_of(node: PathwayNode) -> bool | None:
    """``False`` when the node blocks interruptions, else ``None`` (the call's interruptibility applies)."""
    return False if node.data.model_options.block_interruptions else None


def speak_node(ctx: TurnContext, *, prompt_required: bool) -> HandlerResult:
    """The node's reply: its non-blank ``text`` rendered as :class:`Say`, else the dialogue stream.

    With ``prompt_required`` the stream runs only when the node has a non-blank ``prompt``; otherwise
    nothing is said.
    """
    node = ctx.node
    interruptible = interruptible_of(node)
    if has_content(node.data.text):
        text = ctx.render(node.data.text or "", field_name="text").strip()
        if text:
            return HandlerResult((Say(text=text, mark=ctx.next_mark(), interruptible=interruptible),))
    if prompt_required and not has_content(node.data.prompt):
        return HandlerResult()
    stream = SayStream(tokens=ctx.dialogue.stream(ctx), mark=ctx.next_mark(), interruptible=interruptible)
    return HandlerResult((stream,))


async def _composed(ctx: TurnContext, prompt: str, *, field_name: str) -> str | None:
    instruction = ctx.render(prompt, field_name=field_name).strip()
    if not instruction:
        _log.info("engine.final_line_fallback", node_id=ctx.node.id, reason="blank_instruction")
        return None
    try:
        line = (await ctx.lines.compose(ctx, instruction)).strip()
    except LineUnavailableError as exc:
        _log.warning("engine.final_line_fallback", node_id=ctx.node.id, reason=exc.reason)
        return None
    if not line:
        _log.warning("engine.final_line_fallback", node_id=ctx.node.id, reason="blank")
    return line or None


def _rendered(ctx: TurnContext, text: str | None, *, field_name: str) -> str | None:
    if not has_content(text):
        return None
    return ctx.render(text or "", field_name=field_name).strip() or None


async def final_line(
    ctx: TurnContext, *, speech: str | None, speech_mode: str, speech_prompt: str | None
) -> str | None:
    """The final line of an End Call / Transfer Call node (Decision P07-T06 rule 3).

    ``speechMode: "prompt"`` with a non-blank ``speechPrompt`` composes the line; when that fails it falls
    back to the rendered ``speech``. Without a config line the node's ``text`` is rendered, else its
    ``prompt`` composed. ``None`` = nothing to say.
    """
    line: str | None = None
    if speech_mode == "prompt" and has_content(speech_prompt):
        line = await _composed(ctx, speech_prompt or "", field_name="speechPrompt")
    if line is None:
        line = _rendered(ctx, speech, field_name="speech")
    if line is None:
        line = _rendered(ctx, ctx.node.data.text, field_name="text")
    if line is None and has_content(ctx.node.data.prompt):
        line = await _composed(ctx, ctx.node.data.prompt or "", field_name="prompt")
    return line


def line_actions(ctx: TurnContext, line: str | None) -> tuple[tuple[Action, ...], str | None]:
    """A non-interruptible :class:`Say` of ``line`` and its mark; ``((), None)`` without a line."""
    if line is None:
        return (), None
    mark = ctx.next_mark()
    return (Say(text=line, mark=mark, interruptible=False),), mark
