"""Task-mode runner test helpers: call contexts, a served FakeLLM gateway, output draining, a tool executor.

``async with served() as sim:`` runs FakeLLM on a real socket (wall clock, no latency) behind a va_llm
gateway on a ``ManualClock`` (its deadlines never fire here); ``sim.brain(context, ...)`` builds a
:class:`TaskBrain` on it. :func:`drain` consumes Brain outputs the way the pipeline does (token streams are
read to the end) and renders them as tuples.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, cast

import httpx
import orjson
from llm_client_helpers import GatewayRig, served_gateway, served_requests
from llm_helpers import add_rules, realtime_llm_state
from resolved_configs import resolved_config

from va_common.time import Clock, FrozenClock
from va_domain.call_context import CallContext
from va_domain.org import OrgSettings
from va_engine.system_tools import SystemToolFacts, facts_for_call
from va_engine.task_runner import TaskBrain
from va_engine.templating import TemplateScope
from va_engine.tool_executor import ToolAnswer
from va_llm.models import ToolCall, ToolDef
from va_llm.tokens import default_counter
from va_sim.state import SimState
from va_voice.pipeline import BrainAction, BrainOutput, EndCall, SpeakStatic, SpeakText, UserTurn

__all__ = [
    "AGENT_NUMBER",
    "CALLEE",
    "NOW",
    "RecordingExecutor",
    "Rendered",
    "Sim",
    "assert_tool_pairing",
    "drain",
    "served",
    "task_context",
    "tool_messages",
    "user_turn",
]

NOW: Final = datetime(2026, 11, 2, 15, 30, tzinfo=UTC)
CALLEE: Final = "+14155550100"
AGENT_NUMBER: Final = "+14155550190"

type Rendered = tuple[Any, ...]


def task_context(
    direction: str = "outbound", *, variables: Mapping[str, Any] | None = None, **config: Any
) -> CallContext:
    """A task-mode context from the ``01_task_basic`` snapshot with ``config`` overrides (fresh call id)."""
    return CallContext.model_validate(
        {
            "call_id": uuid.uuid4(),
            "org_id": uuid.uuid4(),
            "direction": direction,
            "to_number": CALLEE if direction != "inbound" else AGENT_NUMBER,
            "from_number": AGENT_NUMBER if direction != "inbound" else CALLEE,
            "config": {**resolved_config(), **config},
            "variables": dict(variables or {}),
            "created_at": NOW,
        }
    )


def user_turn(index: int, text: str, *, dtmf: str | None = None, supersedes: int | None = None) -> UserTurn:
    return UserTurn(
        index=index,
        text=text,
        start_ms=1000.0 * index,
        end_ms=1000.0 * index + 600,
        dtmf=dtmf,
        supersedes=supersedes,
    )


async def drain(outputs: AsyncIterator[BrainOutput]) -> list[Rendered]:
    """Consume ``outputs`` like the pipeline: ``("text", t)`` for non-empty streamed replies, ``("static",
    t)``, ``("end", final, reason)``, ``("action", action)``."""
    rendered: list[Rendered] = []
    async for output in outputs:
        match output:
            case SpeakText():
                text = "".join([delta async for delta in output.tokens])
                if text:
                    rendered.append(("text", text))
            case SpeakStatic():
                rendered.append(("static", output.text, output.interruptible))
            case EndCall():
                rendered.append(("end", output.final_text, output.reason))
            case BrainAction():
                rendered.append(("action", output))
    return rendered


@dataclass(slots=True)
class RecordingExecutor:
    """A (pathway) tool executor: ``refs`` maps node tool ids to tool names; answers ``results[name]``
    (default ``{"ok":name}``) with ``variables[name]``, counts concurrency, holds every call on ``release``
    when ``hold`` is set, and takes ``durations[name]`` seconds on ``clock`` (bind one to time calls).
    ``note`` is the tool usage note speech, ``speeches[name]`` the tool's ``speech`` template; ``started``
    and ``cancelled`` record ``(name, clock ms)``."""

    tools: tuple[ToolDef, ...] = ()
    note: str | None = None
    names: frozenset[str] = frozenset()
    refs: dict[str, str] = field(default_factory=dict)
    results: dict[str, str] = field(default_factory=dict)
    variables: dict[str, dict[str, Any]] = field(default_factory=dict)
    speeches: dict[str, str] = field(default_factory=dict)
    durations: dict[str, float] = field(default_factory=dict)
    failing: frozenset[str] = frozenset()
    hold: bool = False
    clock: Clock | None = None
    calls: list[ToolCall] = field(default_factory=list)
    scopes: list[TemplateScope] = field(default_factory=list)
    started: list[tuple[str, float]] = field(default_factory=list)
    cancelled: list[tuple[str, float]] = field(default_factory=list)
    active: int = 0
    max_active: int = 0
    release: asyncio.Event = field(default_factory=asyncio.Event)
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)

    def definitions(self) -> tuple[ToolDef, ...]:
        return self.tools

    def definitions_for(self, refs: Sequence[str | Mapping[str, Any]]) -> tuple[ToolDef, ...]:
        """The tools named by ``refs`` (a string through ``refs``, a mapping by its ``name``), once each."""
        by_name = {tool.name: tool for tool in self.tools}
        found: dict[str, ToolDef] = {}
        for ref in refs:
            name = self.refs.get(ref) if isinstance(ref, str) else ref.get("name")
            tool = by_name.get(name) if isinstance(name, str) else None
            if tool is not None:
                found.setdefault(tool.name, tool)
        return tuple(found.values())

    @property
    def tool_speech(self) -> str | None:
        return self.note

    def handles(self, name: str) -> bool:
        return name in self.names

    def speech(self, name: str) -> str | None:
        return self.speeches.get(name)

    def _ms(self) -> float:
        return 0.0 if self.clock is None else round(self.clock.monotonic() * 1000, 3)

    async def execute(self, call: ToolCall, scope: TemplateScope) -> ToolAnswer:
        self.calls.append(call)
        self.scopes.append(scope)
        self.started.append((call.name, self._ms()))
        async with self.changed:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.changed.notify_all()
        try:
            if self.hold:
                await self.release.wait()
            duration = self.durations.get(call.name)
            if duration is not None and self.clock is not None:
                await self.clock.sleep(duration)
            if call.name in self.failing:
                message = f"{call.name} failed"
                raise RuntimeError(message)
            content = self.results.get(call.name, orjson.dumps({"ok": call.name}).decode())
            return ToolAnswer(content, self.variables.get(call.name, {}))
        except asyncio.CancelledError:
            self.cancelled.append((call.name, self._ms()))
            raise
        finally:
            self.active -= 1

    async def wait_active(self, count: int) -> None:
        async with asyncio.timeout(5), self.changed:
            await self.changed.wait_for(lambda: self.active >= count)


@dataclass(slots=True)
class Sim:
    rig: GatewayRig
    control: httpx.AsyncClient
    state: SimState
    clock: FrozenClock = field(default_factory=lambda: FrozenClock(NOW))

    def brain(  # noqa: PLR0913 - test knobs, all keyword-only
        self,
        context: CallContext,
        *,
        settings: OrgSettings | None = None,
        facts: SystemToolFacts | None = None,
        executor: RecordingExecutor | None = None,
        memory_context: str | None = None,
        sms_capable: bool = False,
    ) -> TaskBrain:
        org = settings if settings is not None else OrgSettings()
        return TaskBrain(
            context=context,
            settings=org,
            gateway=self.rig.gateway,
            counter=default_counter(),
            facts=facts if facts is not None else facts_for_call(context, org, sms_capable=sms_capable),
            clock=self.clock,
            executor=executor,
            memory_context=memory_context,
        )

    async def rules(self, yaml_text: str) -> None:
        await add_rules(self.control, yaml_text)

    async def requests(self) -> list[dict[str, Any]]:
        return await served_requests(self.control)


@asynccontextmanager
async def served() -> AsyncIterator[Sim]:
    state = realtime_llm_state()
    try:
        async with served_gateway(state) as (rig, control):
            yield Sim(rig, control, state)
    finally:
        await state.aclose()


def tool_messages(request: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The ``tool`` messages of a served request (content parsed)."""
    messages = cast("list[dict[str, Any]]", request["messages"])
    return [
        {**message, "content": orjson.loads(message["content"])}
        for message in messages
        if message["role"] == "tool"
    ]


def assert_tool_pairing(requests: Sequence[Mapping[str, Any]]) -> None:
    """Every assistant message with tool calls is followed by exactly one tool message per call id."""
    for request in requests:
        messages = cast("list[dict[str, Any]]", request["messages"])
        for position, message in enumerate(messages):
            calls = message.get("tool_calls") or []
            if message["role"] != "assistant" or not calls:
                continue
            following = messages[position + 1 : position + 1 + len(calls)]
            assert [m["role"] for m in following] == ["tool"] * len(calls), following
            assert [m["tool_call_id"] for m in following] == [c["id"] for c in calls]
        answered = [m["tool_call_id"] for m in messages if m["role"] == "tool"]
        asked = [c["id"] for m in messages for c in m.get("tool_calls") or []]
        assert answered == asked
