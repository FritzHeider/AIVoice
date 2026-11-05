"""Pathway engine test helpers (specs/11 §5, Decision P07-T08b).

:class:`KeyedPort` answers the decision calls by function name (they run concurrently, so a single FIFO
cannot tell them apart); unscripted calls get neutral answers (``stay``, condition score 0, nothing
extracted). :func:`engine_rig` builds a :class:`PathwayEngine` over it, a :class:`FakeSource` dialogue,
:class:`FakeLines` and a :class:`RecordingEvents`; :func:`drive` runs one engine generator to its end the way
the media worker does (streams read before resuming) and returns the actions as comparable tuples.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import orjson
from dialogue_helpers import CALL_ID, NOW, ORG_ID, FakeSource, dialogue_rig
from handler_helpers import FakeLines, spoken
from pydantic import BaseModel, JsonValue

from va_common.time import FrozenClock
from va_engine.engine import ConversationFacts, EnginePorts, PathwayEngine, new_pathway_state
from va_engine.engine_decide import Decider
from va_engine.handlers import Channel
from va_engine.models import Action, EngineState, PathwayGraph, SayStream
from va_engine.navigation import ActiveGraph, PathwayLoader
from va_engine.ports import HttpPort, SmsPort
from va_engine.system_tools import SystemToolFacts
from va_engine.tool_executor import PathwayToolExecutor
from va_llm.models import ChatMessage
from va_llm.policy import CallType
from va_llm.structured import validate_arguments
from va_voice.pipeline.models import UserTurn

__all__ = [
    "CALLER",
    "PATHWAY_ID",
    "KeyedPort",
    "RecordingEvents",
    "Rig",
    "condition",
    "drive",
    "engine_rig",
    "first_stream",
    "route",
    "user",
]

PATHWAY_ID: Final = uuid.UUID("0191f2a4-7c55-7a1e-9a10-5b2e4c8f1a99")
CALLER: Final = "+14155550100"

type Reply = Mapping[str, Any] | Exception


@dataclass(frozen=True, slots=True)
class Call:
    function_name: str
    call_type: CallType
    messages: tuple[ChatMessage, ...]

    @property
    def user(self) -> str:
        return self.messages[1].content or ""


@dataclass(slots=True)
class KeyedPort:
    """A :class:`~va_engine.decisions.StructuredPort` scripted per function name.

    ``gates`` holds an event per function name: a call waits for it before answering (``started`` is set
    when such a call arrived).
    """

    replies: dict[str, list[Reply]] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)
    gates: dict[str, asyncio.Event] = field(default_factory=dict)
    started: asyncio.Event = field(default_factory=asyncio.Event)

    def script(self, function_name: str, *replies: Reply) -> KeyedPort:
        self.replies.setdefault(function_name, []).extend(replies)
        return self

    def names(self) -> list[str]:
        return [call.function_name for call in self.calls]

    # jscpd:ignore-start  (the fake mirrors the StructuredPort signature)
    async def call_structured[M: BaseModel](  # noqa: PLR0913 - mirrors LlmGateway.call_structured
        self,
        call_type: CallType,
        schema: type[M],
        *,
        org_id: uuid.UUID,
        messages: Sequence[ChatMessage],
        function_name: str,
        description: str,
        call_id: uuid.UUID | None = None,
    ) -> M:
        # jscpd:ignore-end  (signature ends here)
        del org_id, description, call_id
        self.calls.append(Call(function_name, call_type, tuple(messages)))
        gate = self.gates.get(function_name)
        if gate is not None:
            self.started.set()
            await gate.wait()
        queued = self.replies.get(function_name)
        reply = queued.pop(0) if queued else _neutral(function_name, schema)
        if isinstance(reply, Exception):
            raise reply
        return validate_arguments(schema, orjson.dumps(dict(reply)).decode())


def _neutral(function_name: str, schema: type[BaseModel]) -> dict[str, Any]:
    if function_name == "select_path":
        return route("stay", 9)
    if function_name == "score_condition":
        return condition(0)
    return {field.alias or name: None for name, field in schema.model_fields.items()}


def route(path_id: str, confidence: int = 9, *alternatives: tuple[str, int]) -> dict[str, Any]:
    """A ``select_path`` reply."""
    return {
        "path_id": path_id,
        "confidence": confidence,
        "alternatives": [{"path_id": p, "confidence": c} for p, c in alternatives],
        "reason": "scripted",
    }


def condition(score: int) -> dict[str, Any]:
    """A ``score_condition`` reply."""
    return {"score": score, "met": score >= 7, "missing": ""}


@dataclass(slots=True)
class RecordingEvents:
    """Records emitted events in order."""

    events: list[tuple[str, dict[str, JsonValue]]] = field(default_factory=list)

    def emit(self, event_type: str, data: dict[str, JsonValue]) -> None:
        self.events.append((event_type, data))

    def types(self) -> list[str]:
        return [event_type for event_type, _ in self.events]

    def of(self, event_type: str) -> list[dict[str, JsonValue]]:
        return [data for kind, data in self.events if kind == event_type]

    def clear(self) -> None:
        self.events.clear()


@dataclass(slots=True)
class Rig:
    engine: PathwayEngine
    port: KeyedPort
    source: FakeSource
    events: RecordingEvents
    lines: FakeLines
    clock: FrozenClock

    @property
    def state(self) -> EngineState:
        return self.engine.state

    async def start(self, greeting: UserTurn | None = None) -> list[tuple[Any, ...]]:
        return await drive(self.engine.start(greeting))

    async def say(self, text: str) -> list[tuple[Any, ...]]:
        """The caller says ``text`` (the next turn index)."""
        return await drive(self.engine.handle_user_turn(user(text, self.state.turn_index + 1)))

    async def timeout(self) -> list[tuple[Any, ...]]:
        return await drive(self.engine.on_wait_timeout())

    def history(self) -> list[tuple[str, str]]:
        return [(record.role, record.content) for record in self.state.history]


def engine_rig(  # noqa: PLR0913 - test knobs, all keyword-only
    pathway: PathwayGraph,
    *,
    channel: Channel = "voice",
    speculative: bool = True,
    http: HttpPort | None = None,
    sms: SmsPort | None = None,
    loader: PathwayLoader | None = None,
    state: EngineState | None = None,
    port: KeyedPort | None = None,
    facts: ConversationFacts | None = None,
    tools: PathwayToolExecutor | None = None,
    system: SystemToolFacts | None = None,
    fillers: bool = True,
) -> Rig:
    """An engine at the start of ``pathway`` (pathway :data:`PATHWAY_ID` v1) on a frozen clock (``facts``
    replace the default restaurant facts; ``tools``/``system``/``fillers`` set the reply node tools)."""
    clock = FrozenClock(NOW)
    dialogue, source, _ = dialogue_rig(speculative=speculative)
    keyed = port if port is not None else KeyedPort()
    events = RecordingEvents()
    lines = FakeLines()
    active = ActiveGraph(PATHWAY_ID, 1, pathway)
    engine = PathwayEngine(
        state if state is not None else new_pathway_state(active),
        active,
        ports=EnginePorts(
            decider=Decider(keyed, clock=clock),
            dialogue=dialogue,
            lines=lines,
            events=events,
            loader=loader,
            http=http,
            sms=sms,
            tools=tools,
        ),
        facts=facts
        if facts is not None
        else ConversationFacts(
            org_id=ORG_ID,
            call_id=CALL_ID,
            channel=channel,
            request_data={"restaurant_name": "Bella Pasta"},
            builtins={"from": CALLER, "call_id": str(CALL_ID), "user_number": CALLER},
            timezone="UTC",
            system_tools=system,
            tool_fillers=fillers,
        ),
        clock=clock,
    )
    return Rig(engine, keyed, source, events, lines, clock)


def user(text: str, index: int, *, supersedes: int | None = None, dtmf: str | None = None) -> UserTurn:
    return UserTurn(
        index=index,
        text=text,
        start_ms=1000.0 * index,
        end_ms=1000.0 * index + 500,
        dtmf=dtmf,
        supersedes=supersedes,
    )


async def drive(actions: AsyncGenerator[Action]) -> list[tuple[Any, ...]]:
    """Run ``actions`` to the end; every stream is read before the generator resumes."""
    rendered: list[tuple[Any, ...]] = []
    async with contextlib.aclosing(actions) as running:
        async for action in running:
            rendered.extend(await spoken((action,)))
    return rendered


async def first_stream(actions: AsyncGenerator[Action]) -> SayStream:
    """Advance ``actions`` to its first :class:`SayStream` (the generator stays suspended there)."""
    async for action in actions:
        if isinstance(action, SayStream):
            return action
    msg = "no stream was yielded"
    raise AssertionError(msg)
