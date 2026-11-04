"""What the call runtime needs to run a pathway call (specs/07 §16, specs/11 §9, §10; Decision P07-T12b under
specs/11 §10).

:func:`build_pathway_brain` loads the graph of the call's exact ``pathway_version`` through the per-process
:class:`~va_pathways.call_graphs.CallGraphCache` and builds the :class:`~va_engine.pathway_brain.PathwayBrain`
over a :class:`~va_engine.engine.PathwayEngine` whose ports are the LLM gateway (decisions, dialogue
replies, composed final lines), the shared tool HTTP client (Webhook nodes), the call's SMS port, the cache's
loader (forwarding, Transfer Pathway) and :class:`RecorderEvents` (engine events → call events through the
call's :class:`~va_media.turns.TurnRecorder`). A pathway that cannot be loaded raises
:class:`~va_engine.navigation.PathwayUnavailableError` (the runtime ends the call with ``error``).

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
from va_engine.navigation import PathwayUnavailableError
from va_engine.pathway_brain import PathwayBrain
from va_engine.pathway_call import ENGINE_STATE_TTL_S, engine_state_key, pathway_facts, start_state
from va_engine.ports import SmsPort
from va_engine.prompt import DialogueSettings, PathwayPrompt
from va_llm.tokens import TokenCounter
from va_media.turns import TurnRecorder
from va_pathways.call_graphs import CallGraphCache
from va_tools.http import HttpExecutor

__all__ = ["EngineStateSaver", "PathwayGateway", "PathwayTools", "RecorderEvents", "build_pathway_brain"]

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


async def build_pathway_brain(  # noqa: PLR0913 - the call plus its per-process collaborators, keyword-only
    context: CallContext,
    org: OrgSettings,
    *,
    tools: PathwayTools,
    gateway: PathwayGateway,
    counter: TokenCounter,
    clock: Clock,
    sms: SmsPort | None,
    recorder: TurnRecorder | None,
    saver: EngineStateSaver | None,
) -> PathwayBrain:
    """The call's brain on its pinned pathway version (``PathwayUnavailableError`` when it cannot load)."""
    config = context.config
    if config.pathway_id is None or config.pathway_version is None:
        raise PathwayUnavailableError("not_found")
    active = await tools.graphs.get(context.org_id, config.pathway_id, config.pathway_version)
    prompt = PathwayPrompt(DialogueSettings.for_call(context, org), counter=counter, clock=clock)
    engine = PathwayEngine(
        start_state(active, context),
        active,
        ports=EnginePorts(
            decider=Decider(gateway, clock=clock),
            dialogue=GatewayDialogue(gateway, prompt),
            lines=GatewayLinePort(gateway, org_id=context.org_id, call_id=context.call_id),
            events=RecorderEvents(recorder),
            loader=tools.graphs.loader(context.org_id),
            http=HttpExecutor(
                tools.tools_http, clock=clock, allow_http=tools.allow_http, call_id=str(context.call_id)
            ),
            sms=sms,
        ),
        facts=pathway_facts(context, now=clock.now()),
        clock=clock,
    )
    brain = PathwayBrain(engine, clock=clock, on_state=None if saver is None else saver.on_state)
    if recorder is not None:
        recorder.tagger = brain
    return brain
