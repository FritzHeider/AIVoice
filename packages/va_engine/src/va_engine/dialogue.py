"""The pathway dialogue port over the LLM gateway (specs/11 §5.7, specs/10 §2; Decision P07-T08a rule 7).

:class:`GatewayDialogue` implements :class:`va_engine.handlers.DialoguePort`. Each :meth:`~GatewayDialogue.
stream` opens one streaming request at the context's node (or relays a committed speculative stream handed
over with :meth:`~GatewayDialogue.adopt`) and yields its deltas. A failure before the first token ends the
iterator empty and is kept in :attr:`~GatewayDialogue.unavailable` (the engine then speaks ``errorPhrase``).
The stream stays open after its last delta so the turn orchestration can read
:meth:`~va_llm.dialogue.DialogueStream.tool_calls` from :attr:`~GatewayDialogue.last`; closing the iterator
early closes the stream, and :meth:`~GatewayDialogue.aclose` closes whatever is still open.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from va_engine.handlers.base import TurnContext
from va_engine.prompt import PathwayPrompt
from va_llm.dialogue import DialogueRequest, DialogueStream
from va_llm.errors import LlmUnavailableError
from va_llm.models import ChatMessage, ToolDef
from va_llm.prompts import DEFAULT_TOOL_SPEECH

__all__ = ["DialogueSource", "GatewayDialogue"]


class DialogueSource(Protocol):
    """Where replies stream from (:class:`va_llm.LlmGateway` satisfies it)."""

    async def stream_dialogue(self, request: DialogueRequest) -> DialogueStream: ...


class GatewayDialogue:
    """The dialogue port of one pathway conversation.

    The turn orchestration sets the per-turn inputs before a handler streams: ``condition_suffix`` (an unmet
    loop condition), ``tools`` (offered functions) and ``extra`` (in-turn messages after the history).
    """

    def __init__(self, source: DialogueSource, prompt: PathwayPrompt) -> None:
        self._source = source
        self._prompt = prompt
        self.condition_suffix: str | None = None
        self.tools: tuple[ToolDef, ...] = ()
        self.tool_speech: str = DEFAULT_TOOL_SPEECH
        self.extra: list[ChatMessage] = []
        self._last: DialogueStream | None = None
        self._adopted: DialogueStream | None = None
        self._parts: list[str] = []
        self._unavailable: LlmUnavailableError | None = None

    @property
    def prompt(self) -> PathwayPrompt:
        return self._prompt

    @property
    def last(self) -> DialogueStream | None:
        """The stream of the latest :meth:`stream` (``None`` before the first)."""
        return self._last

    @property
    def unavailable(self) -> LlmUnavailableError | None:
        """Why the latest stream produced no token (``None`` when it did or is still running)."""
        return self._unavailable

    @property
    def text(self) -> str:
        """The deltas the latest stream yielded so far."""
        return "".join(self._parts)

    def request(self, ctx: TurnContext, *, speculative: bool = False) -> DialogueRequest:
        """The request at the context's node with the current per-turn inputs."""
        return self._prompt.request(
            ctx,
            condition_suffix=self.condition_suffix,
            tools=self.tools,
            tool_speech=self.tool_speech,
            extra=self.extra,
            speculative=speculative,
        )

    async def open(self, ctx: TurnContext, *, speculative: bool = False) -> DialogueStream:
        """Start a request at the context's node (returns before the first token)."""
        return await self._source.stream_dialogue(self.request(ctx, speculative=speculative))

    def adopt(self, stream: DialogueStream) -> None:
        """The next :meth:`stream` relays ``stream`` (a committed speculative one) instead of opening one."""
        self._adopted = stream

    def stream(self, ctx: TurnContext) -> AsyncIterator[str]:
        """The reply's deltas at the context's node."""
        return self._relay(ctx)

    async def aclose(self) -> None:
        """Close the latest stream and an adopted one nobody read."""
        adopted, self._adopted = self._adopted, None
        for stream in (adopted, self._last):
            if stream is not None:
                await stream.aclose()

    async def _relay(self, ctx: TurnContext) -> AsyncIterator[str]:
        self._parts = []
        self._unavailable = None
        stream, self._adopted = self._adopted, None
        if stream is None:
            stream = await self.open(ctx)
        self._last = stream
        finished = False
        try:
            async for delta in stream.text():
                self._parts.append(delta)
                yield delta
            finished = True
        except LlmUnavailableError as exc:
            self._unavailable = exc
        finally:
            if not finished:
                await stream.aclose()
