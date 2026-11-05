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
from va_engine.tool_executor import ToolAnswer, ToolRef
from va_llm.models import ToolCall, ToolDef
from va_llm.tokens import default_counter
from va_sim.state import SimState
from va_voice.pipeline import BrainAction, BrainOutput, EndCall, SpeakStatic, SpeakText, UserTurn

__all__ = [
    "AGENT_NUMBER",
    "CALLEE",
    "NOW",
    "NodeExecutor",
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


def _resolve(
    ref: ToolRef, ids: Mapping[str, str], by_name: Mapping[str, ToolDef]
) -> tuple[ToolDef, str] | None:
    """The definition ``ref`` names and its label (``None`` = unresolved)."""
    if isinstance(ref, str):
        tool = by_name.get(ids.get(ref, ""))
        return None if tool is None else (tool, ref)
    name = ref.get("name")
    if not isinstance(name, str):
        return None
    description, url = ref.get("description"), ref.get("url")
    if description is None and url is None:
        tool = by_name.get(name)
        return None if tool is None else (tool, name)
    inline = ToolDef(
        name=name,
        description=description if isinstance(description, str) else "",
        parameters={"type": "object"},
    )
    return inline, url if isinstance(url, str) else name


@dataclass(slots=True)
class NodeExecutor:
    """One node's tools of a :class:`RecordingExecutor`: its definitions and which of them ran."""

    parent: RecordingExecutor
    tools: tuple[ToolDef, ...]
    labels: dict[str, str]

    def definitions(self) -> tuple[ToolDef, ...]:
        return self.tools

    @property
    def tool_speech(self) -> str | None:
        return self.parent.tool_speech

    def handles(self, name: str) -> bool:
        return name in self.labels or self.parent.handles(name)

    def speech(self, name: str) -> str | None:
        return self.parent.speech(name)

    def detached(self, call: ToolCall) -> bool:
        return self.parent.detached(call)

    async def execute(self, call: ToolCall, scope: TemplateScope) -> ToolAnswer:
        self.parent.ran.append((self.labels.get(call.name, call.name), call.name))
        return await self.parent.execute(call, scope)


@dataclass(slots=True)
class RecordingExecutor:
    """A (pathway) tool executor: ``refs`` maps node tool ids to tool names; answers ``results[name]``
    (default ``{"ok":name}``) with ``variables[name]``, counts concurrency, holds every call on ``release``
    when ``hold`` is set (only the tools named in ``holding`` otherwise), and takes ``durations[name]``
    seconds on ``clock`` (bind one to time calls).
    ``note`` is the tool usage note speech, ``speeches[name]`` the tool's ``speech`` template; ``started``
    and ``cancelled`` record ``(name, clock ms)``. Runs of the tools named in ``detaching`` go on when their
    wait is cancelled (like an executor-owned system tool): such calls are listed in ``left_running`` and
    reported by :meth:`detached`. :meth:`for_refs` gives a node its :class:`NodeExecutor`, whose runs are
    listed in ``ran`` as ``(label of the definition that ran, tool name)``."""

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
    holding: frozenset[str] = frozenset()
    detaching: frozenset[str] = frozenset()
    clock: Clock | None = None
    calls: list[ToolCall] = field(default_factory=list)
    ran: list[tuple[str, str]] = field(default_factory=list)
    scopes: list[TemplateScope] = field(default_factory=list)
    started: list[tuple[str, float]] = field(default_factory=list)
    cancelled: list[tuple[str, float]] = field(default_factory=list)
    left_running: list[ToolCall] = field(default_factory=list)
    active: int = 0
    max_active: int = 0
    release: asyncio.Event = field(default_factory=asyncio.Event)
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)

    def definitions(self) -> tuple[ToolDef, ...]:
        return self.tools

    def for_refs(self, refs: Sequence[ToolRef]) -> NodeExecutor:
        """The node executor of ``refs``, first ref per tool name.

        A string resolves through ``refs`` to one of ``tools`` (label: the ref). A mapping with a
        ``description`` or ``url`` is its own inline definition (label: its ``url``, else its name); a bare
        ``{"name": ...}`` mapping resolves to the tool of that name (label: the name).
        """
        by_name = {tool.name: tool for tool in self.tools}
        found: dict[str, ToolDef] = {}
        labels: dict[str, str] = {}
        for ref in refs:
            resolved = _resolve(ref, self.refs, by_name)
            if resolved is not None and resolved[0].name not in found:
                tool, label = resolved
                found[tool.name] = tool
                labels[tool.name] = label
        return NodeExecutor(self, tuple(found.values()), labels)

    @property
    def tool_speech(self) -> str | None:
        return self.note

    def handles(self, name: str) -> bool:
        return name in self.names

    def speech(self, name: str) -> str | None:
        return self.speeches.get(name)

    def detached(self, call: ToolCall) -> bool:
        return any(item is call for item in self.left_running)

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
            if self.hold or call.name in self.holding:
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
            if call.name in self.detaching:
                self.left_running.append(call)
            raise
        finally:
            self.active -= 1

    async def wait_active(self, count: int) -> None:
        async with asyncio.timeout(5), self.changed:
            await self.changed.wait_for(lambda: self.active >= count)

    async def wait_started(self, name: str) -> None:
        """Until a run of tool ``name`` started."""
        async with asyncio.timeout(5), self.changed:
            await self.changed.wait_for(lambda: any(started == name for started, _ in self.started))


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
