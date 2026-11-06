"""What the call runtime needs to run a pathway call (specs/07 §16, specs/11 §9, §10; Decision P07-T12b under
specs/11 §10; tools: Decision P08-T02c2 under specs/13 §4).

:func:`load_call_graph` loads the graph of the call's exact ``pathway_version`` through the per-process
:class:`~va_pathways.call_graphs.CallGraphCache`; a pathway that cannot be loaded raises
:class:`~va_engine.navigation.PathwayUnavailableError` (the runtime ends the call with ``error``).
:func:`build_pathway_brain` builds the :class:`~va_engine.pathway_brain.PathwayBrain` over a
:class:`~va_engine.engine.PathwayEngine` on that graph and the call's start state. The engine's ports are the
LLM gateway (decisions, dialogue replies, composed final lines), the shared tool HTTP client (Webhook nodes),
the call's SMS port, the cache's loader (forwarding, Transfer Pathway) and :class:`RecorderEvents` (engine
events → call events through the call's :class:`~va_media.turns.TurnRecorder`). With the call's
:class:`~va_media.call_tools.PathwayCallTools` its reply nodes run their custom tools and the call's
executor-backed system tools, and the loader also loads the stored tools of every graph the call switches to;
``system_tools`` are the system tools the call allows its reply nodes. ``turn_data`` is the call's per-turn
dynamic data (specs/13 §7 Decision P08-T03b): the engine refreshes it at every caller turn and the reply
prompt reads its context lines.

:class:`PathwayToolEvents` reports the call's tool runs through the recorder; ``call.tool`` also carries the
engine turn and node the run was reported at (``turn_index``, ``node_id``), which is what lists it in the
turn's pathway log (specs/11 §12).

:class:`EngineStateSaver` is the brain's ``on_state`` callback: it writes the latest
:class:`~va_engine.models.EngineState` to ``call:{id}:engine`` (``dump_engine_state``, kept
:data:`~va_engine.pathway_call.ENGINE_STATE_TTL_S`) in one ordered background chain, latest state winning; a
Redis failure is logged ``media.engine_state_save_failed`` and never reaches the call. The end routine reads
the final snapshot's variables into ``calls.variables`` (:mod:`va_media.call_end`).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Final, Protocol

import httpx
from pydantic import JsonValue
from redis.asyncio import Redis
from redis.exceptions import RedisError

from va_common.logging import get_logger
from va_common.time import Clock
from va_domain.call_context import CallContext
from va_domain.org import OrgSettings
from va_engine.decisions import StructuredPort
from va_engine.dialogue import DialogueSource, GatewayDialogue
from va_engine.engine import EnginePorts, PathwayEngine
from va_engine.engine_decide import Decider
from va_engine.final_lines import CompletionPort, GatewayLinePort
from va_engine.models import EngineState, dump_engine_state
from va_engine.navigation import ActiveGraph, PathwayUnavailableError
from va_engine.pathway_brain import PathwayBrain
from va_engine.pathway_call import ENGINE_STATE_TTL_S, engine_state_key, pathway_facts
from va_engine.ports import SmsPort
from va_engine.prompt import DialogueSettings, PathwayPrompt
from va_engine.system_tools import SystemToolFacts
from va_engine.turn_data import TurnData
from va_llm.tokens import TokenCounter
from va_media.call_tools import PathwayCallTools
from va_media.turns import TurnRecorder
from va_pathways.call_graphs import CallGraphCache
from va_tools.http import HttpExecutor

__all__ = [
    "EngineStateSaver",
    "PathwayGateway",
    "PathwayToolEvents",
    "PathwayTools",
    "RecorderEvents",
    "build_pathway_brain",
    "load_call_graph",
]

_SAVE_FAILED: Final = "media.engine_state_save_failed"

_log = get_logger(__name__)


class PathwayGateway(DialogueSource, StructuredPort, CompletionPort, Protocol):
    """The LLM gateway of a pathway call (:class:`va_llm.LlmGateway` satisfies it)."""


@dataclass(frozen=True, slots=True)
class PathwayTools:
    """The per-process pieces of pathway calls: the graph cache, the shared tool HTTP client, and whether
    webhook URLs may be ``http://``/private (``local``/``test`` only)."""

    graphs: CallGraphCache
    tools_http: httpx.AsyncClient
    allow_http: bool


class RecorderEvents:
    """The engine's :class:`~va_engine.engine_events.EngineEvents`: events go to the call's recorder (none:
    dropped, the call keeps no events)."""

    def __init__(self, recorder: TurnRecorder | None) -> None:
        self._recorder = recorder

    def emit(self, event_type: str, data: dict[str, JsonValue]) -> None:
        if self._recorder is not None:
            self._recorder.engine_event(event_type, data)


class PathwayToolEvents:
    """The :class:`~va_tools.executor.ToolEvents` of a pathway call: tool runs go to the call's recorder,
    ``call.tool`` stamped with the engine turn and node at report time. Holds the recorder and the call's
    one state object only (never the engine or the brain)."""

    def __init__(self, recorder: TurnRecorder, state: EngineState) -> None:
        self._recorder = recorder
        self._state = state

    def tool(self, data: dict[str, JsonValue]) -> None:
        state = self._state
        self._recorder.tool({**data, "turn_index": state.turn_index, "node_id": state.current_node_id})

    def tool_failed(self, detail: str) -> None:
        self._recorder.tool_failed(detail)

    def sms_sent(self, data: dict[str, JsonValue]) -> None:
        self._recorder.sms_sent(data)


class EngineStateSaver:
    """Snapshots a call's engine state in Redis (see the module docstring)."""

    def __init__(self, redis: Redis, call_id: uuid.UUID) -> None:
        self._redis = redis
        self._key = engine_state_key(call_id)
        self._latest: str | None = None
        self._writer: asyncio.Task[None] | None = None

    def on_state(self, state: EngineState) -> None:
        """Queue a write of ``state`` (serialized now, so later changes are not part of this snapshot)."""
        self._latest = dump_engine_state(state)
        if self._writer is None or self._writer.done():
            self._writer = asyncio.get_running_loop().create_task(self._write(), name="engine-state-save")

    async def aclose(self) -> None:
        """Wait until the latest state was written (or failed)."""
        writer = self._writer
        if writer is not None:
            await asyncio.wait({writer})

    async def _write(self) -> None:
        while self._latest is not None:
            payload, self._latest = self._latest, None
            try:
                await self._redis.set(self._key, payload, ex=ENGINE_STATE_TTL_S)
            except (RedisError, OSError) as exc:
                _log.warning(_SAVE_FAILED, error_type=type(exc).__name__)


async def load_call_graph(context: CallContext, tools: PathwayTools) -> ActiveGraph:
    """The graph of the call's pinned pathway version (``PathwayUnavailableError`` when it cannot load)."""
    config = context.config
    if config.pathway_id is None or config.pathway_version is None:
        raise PathwayUnavailableError("not_found")
    return await tools.graphs.get(context.org_id, config.pathway_id, config.pathway_version)


def build_pathway_brain(  # noqa: PLR0913 - the call plus its per-process collaborators, keyword-only
    context: CallContext,
    org: OrgSettings,
    *,
    active: ActiveGraph,
    state: EngineState,
    tools: PathwayTools,
    gateway: PathwayGateway,
    counter: TokenCounter,
    clock: Clock,
    sms: SmsPort | None,
    recorder: TurnRecorder | None,
    saver: EngineStateSaver | None,
    call_tools: PathwayCallTools | None = None,
    system_tools: SystemToolFacts | None = None,
    turn_data: TurnData | None = None,
) -> PathwayBrain:
    """The call's brain on ``active`` (its pinned version, :func:`load_call_graph`) starting at ``state``
    (:func:`~va_engine.pathway_call.start_state`)."""
    prompt = PathwayPrompt(
        DialogueSettings.for_call(context, org), counter=counter, clock=clock, turn_data=turn_data
    )
    loader = tools.graphs.loader(context.org_id)
    engine = PathwayEngine(
        state,
        active,
        ports=EnginePorts(
            decider=Decider(gateway, clock=clock),
            dialogue=GatewayDialogue(gateway, prompt),
            lines=GatewayLinePort(gateway, org_id=context.org_id, call_id=context.call_id),
            events=RecorderEvents(recorder),
            loader=loader if call_tools is None else call_tools.loader(loader),
            http=HttpExecutor(
                tools.tools_http, clock=clock, allow_http=tools.allow_http, call_id=str(context.call_id)
            ),
            sms=sms,
            tools=None if call_tools is None else call_tools.executor,
            turn_data=turn_data,
        ),
        facts=pathway_facts(
            context, now=clock.now(), system_tools=system_tools, tool_fillers=org.engine.tool_fillers
        ),
        clock=clock,
    )
    brain = PathwayBrain(engine, clock=clock, on_state=None if saver is None else saver.on_state)
    if recorder is not None:
        recorder.tagger = brain
    return brain
