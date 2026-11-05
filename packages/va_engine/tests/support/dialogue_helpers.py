"""Pathway dialogue test helpers: a scripted ``DialogueStream`` double, a recording source, prompt rigs.

:class:`FakeStream` follows the :class:`va_llm.dialogue.DialogueStream` consumer contract: a speculative
stream yields nothing until ``commit()``; ``discard()`` ends ``text()`` empty; ``fail`` is raised from
``text()`` after the deltas (an :class:`LlmUnavailableError` with no deltas = a pre-token failure). It is
also an async context manager (closing on exit), as :class:`va_engine.task_runner.TaskBrain` uses streams.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, cast

from va_common.time import FrozenClock
from va_engine.dialogue import GatewayDialogue
from va_engine.prompt import DialogueSettings, PathwayPrompt
from va_llm.dialogue import DialogueRequest, DialogueStream
from va_llm.errors import LlmUnavailableError
from va_llm.models import ToolCall
from va_llm.tokens import TokenCounter, default_counter

__all__ = [
    "CALL_ID",
    "NOW",
    "ORG_ID",
    "FakeSource",
    "FakeStream",
    "counter",
    "dialogue_rig",
    "dialogue_settings",
    "unavailable",
]

NOW: Final = datetime(2026, 11, 3, 17, 45, tzinfo=UTC)
ORG_ID: Final = uuid.UUID("0191f2a4-7c55-7a1e-9a10-5b2e4c8f1b01")
CALL_ID: Final = uuid.UUID("0191f2a4-7c55-7a1e-9a10-5b2e4c8f1b02")

_COUNTER: TokenCounter | None = None


def counter() -> TokenCounter:
    """One token counter per test process (loading the encoding is slow)."""
    global _COUNTER  # noqa: PLW0603 - a lazily built process-wide cache
    if _COUNTER is None:
        _COUNTER = default_counter()
    return _COUNTER


def unavailable() -> LlmUnavailableError:
    return LlmUnavailableError("exhausted", provider_id="platform", model="dialogue", last=None)


class FakeStream:
    """A scripted stream; records what the consumer did with it."""

    def __init__(
        self,
        request: DialogueRequest,
        deltas: Sequence[str] = (),
        *,
        fail: Exception | None = None,
        calls: Sequence[ToolCall] = (),
    ) -> None:
        self.request = request
        self.deltas = list(deltas)
        self.fail = fail
        self.calls = list(calls)
        self.committed = False
        self.discarded = False
        self.closed = 0
        self.iterated = False
        self.yielded: list[str] = []
        self.error: Exception | None = None
        self._released = asyncio.Event()
        if not request.speculative:
            self._released.set()

    def text(self) -> AsyncIterator[str]:
        self.iterated = True
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[str]:
        await self._released.wait()
        for delta in self.deltas:
            if self.discarded or self.closed:
                return
            self.yielded.append(delta)
            yield delta
        if self.fail is not None and not self.discarded:
            raise self.fail

    async def tool_calls(self) -> list[ToolCall]:
        return list(self.calls)

    def commit(self) -> None:
        self.committed = True
        self._released.set()

    async def discard(self) -> None:
        self.discarded = True
        self._released.set()

    async def aclose(self) -> None:
        self.closed += 1

    async def __aenter__(self) -> FakeStream:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


@dataclass(slots=True)
class _Script:
    deltas: Sequence[str]
    fail: Exception | None = None
    calls: Sequence[ToolCall] = ()


@dataclass(slots=True)
class FakeSource:
    """Returns scripted :class:`FakeStream`\\ s in order (an empty reply once the script ran out)."""

    script: list[_Script] = field(default_factory=list)
    streams: list[FakeStream] = field(default_factory=list)

    def reply(
        self, *deltas: str, fail: Exception | None = None, calls: Sequence[ToolCall] = ()
    ) -> FakeSource:
        self.script.append(_Script(deltas, fail, calls))
        return self

    @property
    def requests(self) -> list[DialogueRequest]:
        return [stream.request for stream in self.streams]

    async def stream_dialogue(self, request: DialogueRequest) -> DialogueStream:
        step = self.script.pop(0) if self.script else _Script(())
        stream = FakeStream(request, step.deltas, fail=step.fail, calls=step.calls)
        self.streams.append(stream)
        return cast("DialogueStream", stream)


def dialogue_settings(**over: Any) -> DialogueSettings:
    """Fixed settings of an outbound call (``over`` replaces fields)."""
    values: dict[str, Any] = {
        "org_id": ORG_ID,
        "call_id": CALL_ID,
        "direction": "outbound",
        "counterpart": "+14155550100",
        "language_name": "English",
        "dialogue_model": "call-model",
        "temperature": 0.4,
        "max_tokens": 250,
        "context_budget_tokens": 6_000,
        "speculative": True,
    }
    values.update(over)
    return DialogueSettings(**values)


def dialogue_rig(
    source: FakeSource | None = None, **settings: Any
) -> tuple[GatewayDialogue, FakeSource, PathwayPrompt]:
    """A :class:`GatewayDialogue` over a :class:`FakeSource` on a frozen clock at :data:`NOW`."""
    fake = source if source is not None else FakeSource()
    prompt = PathwayPrompt(dialogue_settings(**settings), counter=counter(), clock=FrozenClock(NOW))
    return GatewayDialogue(fake, prompt), fake, prompt
