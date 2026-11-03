"""The pathway test chat: a text simulation of a pathway version (specs/12 §7; Decision P07-T10 there).

A chat is one Redis document ``pathway_chat:{chat_id}`` (:class:`ChatSession`, TTL 24 h refreshed on every
write): the frozen graph of the version it started on, the request data, the simulated numbers, the engine
state, and the state after each answered turn (``snapshots``, newest :data:`MAX_SNAPSHOTS`), which is what
``branch_from_turn`` forks from. Every request builds a :class:`~va_engine.engine.PathwayEngine` on the
stored state on channel ``voice`` (the chat simulates a call), runs it through a
:class:`~va_api.services.pathway_chat_run.ChatRecorder`, and stores the state it left. Webhook nodes call the
customer's endpoint for real (``va_tools.http.HttpExecutor``); SMS, transfers, end call, press button and
human takeover are only recorded. A turn is capped at :data:`TURN_TIMEOUT_S` of the container clock.
Concurrent sends to one chat are refused while ``pathway_chat:{chat_id}:lock`` is held.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import httpx
import orjson
from pydantic import BaseModel, JsonValue
from redis.asyncio import Redis
from redis.exceptions import RedisError

from va_api.auth.context import RequestContext
from va_api.services.pathway_chat_run import ChatRecorder, Item
from va_api.services.pathway_loader import DbPathwayLoader
from va_api.services.pathways import VersionSelector, load_version
from va_common.errors import AppError
from va_common.ids import new_id
from va_common.logging import get_logger
from va_common.time import Clock
from va_common.waits import within
from va_db.org_settings import OrgSettingsReader
from va_db.repositories import PathwayGraphJson
from va_db.session import Database
from va_domain.org import OrgSettings
from va_engine.builtins import builtin_values
from va_engine.dialogue import GatewayDialogue
from va_engine.engine import ConversationFacts, EnginePorts, PathwayEngine, new_pathway_state
from va_engine.engine_decide import Decider
from va_engine.final_lines import GatewayLinePort
from va_engine.models import (
    EngineState,
    PathwayGraph,
    dump_engine_state,
    dump_graph,
    load_engine_state,
    load_graph,
)
from va_engine.models.results import Action
from va_engine.navigation import ActiveGraph
from va_engine.prompt import DialogueSettings, PathwayPrompt
from va_llm import LlmGateway
from va_llm.tokens import TokenCounter, default_counter
from va_tools.http import HttpExecutor
from va_voice.pipeline.models import UserTurn

__all__ = [
    "BUSY_MESSAGE",
    "CHAT_LLM_TIMEOUT",
    "CHAT_TTL_S",
    "DEFAULT_SIMULATE_FROM",
    "DEFAULT_SIMULATE_TO",
    "DELETED_MESSAGE",
    "ENDED_MESSAGE",
    "LOCK_TTL_S",
    "MAX_SNAPSHOTS",
    "NOT_FOUND_MESSAGE",
    "TURN_TIMEOUT_S",
    "ChatDeps",
    "ChatSession",
    "ChatStart",
    "ChatStore",
    "ChatTurn",
    "chat_key",
    "create_chat",
    "delete_chat",
    "lock_key",
    "prune_snapshots",
    "send_message",
]

CHAT_TTL_S: Final = 86_400
LOCK_TTL_S: Final = 120
TURN_TIMEOUT_S: Final = 60.0
MAX_SNAPSHOTS: Final = 50
CHAT_LLM_TIMEOUT: Final = httpx.Timeout(30.0, connect=2.0)
DEFAULT_SIMULATE_FROM: Final = "+14155550123"
DEFAULT_SIMULATE_TO: Final = "+14155550100"
CHAT_LANGUAGE: Final = "en-US"
CHAT_TIMEZONE: Final = "UTC"
NOT_FOUND_MESSAGE: Final = "Chat not found."
ENDED_MESSAGE: Final = "This chat has ended."
BUSY_MESSAGE: Final = "This chat is answering another message."
DELETED_MESSAGE: Final = "Chat deleted."
UNAVAILABLE_MESSAGE: Final = "Test chat storage is unavailable; try again."
NO_START_MESSAGE: Final = "The pathway has no start node; pass start_node_id."
UNKNOWN_START_MESSAGE: Final = "start_node_id is not a node of this pathway version."
NO_SNAPSHOT_MESSAGE: Final = "That turn is not available to branch from."

_RELEASE_LOCK: Final = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end return 0"
)

_log = get_logger(__name__)


def chat_key(chat_id: uuid.UUID) -> str:
    """``pathway_chat:{chat_id}`` (specs/03 Redis keys)."""
    return f"pathway_chat:{chat_id}"


def lock_key(chat_id: uuid.UUID) -> str:
    return f"pathway_chat:{chat_id}:lock"


class ChatSession(BaseModel):
    """The stored chat: ``graph`` is the dumped graph of version ``version``; ``state`` and ``snapshots``
    (turn index → state) are engine-state documents."""

    org_id: uuid.UUID
    pathway_id: uuid.UUID
    version: int
    graph: dict[str, Any]
    request_data: dict[str, JsonValue]
    simulate_from: str
    simulate_to: str
    created_at: datetime
    state: dict[str, Any]
    snapshots: dict[str, dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ChatTurn:
    """What a request produced; ``chat_id`` is a new chat after branching."""

    chat_id: uuid.UUID
    assistant_messages: list[str]
    current_node_id: str
    variables: dict[str, JsonValue]
    actions: list[Item]
    ended: bool
    logs: list[Item]


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatStart:
    """A validated create request (numbers already E.164)."""

    pathway_id: uuid.UUID
    version: VersionSelector
    request_data: dict[str, JsonValue]
    start_node_id: str | None
    simulate_from: str
    simulate_to: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatDeps:
    """The container pieces the chat runs on."""

    redis: Redis
    database: Database
    gateway: LlmGateway
    tools_http: httpx.AsyncClient
    org_settings: OrgSettingsReader
    clock: Clock
    allow_http: bool


def _unavailable(exc: RedisError) -> AppError:
    _log.warning("pathway_chat.redis_unavailable", error=type(exc).__name__)
    return AppError("SERVICE_UNAVAILABLE", UNAVAILABLE_MESSAGE)


def prune_snapshots(snapshots: Mapping[str, dict[str, Any]], limit: int = MAX_SNAPSHOTS) -> dict[str, Any]:
    """The newest ``limit`` snapshots by turn index."""
    kept = sorted(snapshots, key=int)[-limit:]
    return {key: snapshots[key] for key in kept}


class ChatStore:
    """The chat documents in Redis (every failure is ``SERVICE_UNAVAILABLE``)."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def load(self, org_id: uuid.UUID, chat_id: uuid.UUID) -> ChatSession:
        """The org's chat; ``NOT_FOUND`` when absent, expired, or of another org."""
        try:
            raw = await self._redis.get(chat_key(chat_id))
        except RedisError as exc:
            raise _unavailable(exc) from exc
        if raw is None:
            raise AppError("NOT_FOUND", NOT_FOUND_MESSAGE)
        session = ChatSession.model_validate_json(raw)
        if session.org_id != org_id:
            raise AppError("NOT_FOUND", NOT_FOUND_MESSAGE)
        return session

    async def save(self, chat_id: uuid.UUID, session: ChatSession) -> None:
        try:
            await self._redis.set(chat_key(chat_id), session.model_dump_json(), ex=CHAT_TTL_S)
        except RedisError as exc:
            raise _unavailable(exc) from exc

    async def delete(self, org_id: uuid.UUID, chat_id: uuid.UUID) -> None:
        """Delete the org's chat (``NOT_FOUND`` as :meth:`load`)."""
        await self.load(org_id, chat_id)
        try:
            await self._redis.delete(chat_key(chat_id))
        except RedisError as exc:
            raise _unavailable(exc) from exc

    @asynccontextmanager
    async def lock(self, chat_id: uuid.UUID) -> AsyncIterator[None]:
        """Hold the chat's answer lock (``CONFLICT`` while another request holds it)."""
        token = secrets.token_hex(16)
        key = lock_key(chat_id)
        try:
            acquired = await self._redis.set(key, token, nx=True, ex=LOCK_TTL_S)
        except RedisError as exc:
            raise _unavailable(exc) from exc
        if not acquired:
            raise AppError("CONFLICT", BUSY_MESSAGE)
        try:
            yield
        finally:
            try:
                await self._redis.eval(_RELEASE_LOCK, 1, key, token)
            except RedisError as exc:
                _log.warning("pathway_chat.unlock_failed", error=type(exc).__name__)


def _state_document(state: EngineState) -> dict[str, Any]:
    document: dict[str, Any] = orjson.loads(dump_engine_state(state))
    return document


def _graph_of(session: ChatSession) -> PathwayGraph:
    graph = session.graph
    return load_graph(graph["nodes"], graph["edges"], graph["global_config"])


async def _org_settings(deps: ChatDeps, org_id: uuid.UUID) -> OrgSettings:
    settings = await deps.org_settings.get_lenient(org_id)
    return OrgSettings() if settings is None else settings


class _Conversation:
    """One request's engine over ``state`` with the recorder on every port."""

    def __init__(  # noqa: PLR0913 - the request's chat, graph, state, org settings and dependencies
        self,
        deps: ChatDeps,
        *,
        chat_id: uuid.UUID,
        session: ChatSession,
        graph: PathwayGraph,
        state: EngineState,
        org: OrgSettings,
        counter: TokenCounter,
    ) -> None:
        self.recorder = ChatRecorder(
            HttpExecutor(deps.tools_http, clock=deps.clock, allow_http=deps.allow_http, call_id=str(chat_id))
        )
        self._deps = deps
        settings = DialogueSettings(
            org_id=session.org_id,
            direction="inbound",
            call_id=chat_id,
            counterpart=session.simulate_from,
            context_budget_tokens=org.llm.context_budget_tokens,
            speculative=org.engine.speculative,
        )
        prompt = PathwayPrompt(settings, counter=counter, clock=deps.clock)
        self.engine = PathwayEngine(
            state,
            ActiveGraph(session.pathway_id, session.version, graph),
            ports=EnginePorts(
                decider=Decider(deps.gateway, clock=deps.clock),
                dialogue=GatewayDialogue(deps.gateway, prompt),
                lines=GatewayLinePort(deps.gateway, org_id=session.org_id, call_id=chat_id),
                events=self.recorder,
                loader=DbPathwayLoader(deps.database, session.org_id),
                http=self.recorder,
                sms=self.recorder,
            ),
            facts=ConversationFacts(
                org_id=session.org_id,
                call_id=chat_id,
                channel="voice",
                request_data=session.request_data,
                builtins=builtin_values(
                    now=deps.clock.now(),
                    timezone=CHAT_TIMEZONE,
                    from_number=session.simulate_from,
                    to_number=session.simulate_to,
                    call_id=str(chat_id),
                    voice_id=None,
                    language=CHAT_LANGUAGE,
                    inbound=True,
                ),
                timezone=CHAT_TIMEZONE,
            ),
            clock=deps.clock,
        )

    async def run(self, chat_id: uuid.UUID, actions: AsyncGenerator[Action]) -> None:
        """Consume ``actions`` within the turn cap, then settle and close the engine."""
        try:
            finished = await within(self.recorder.consume(actions), self._deps.clock, TURN_TIMEOUT_S)
            if not finished:
                _log.warning("pathway_chat.turn_timeout", chat_id=str(chat_id))
            await self.engine.settle_background()
        finally:
            await self.engine.aclose()
        if self.recorder.terminal:
            self.engine.state.ended = True

    def turn(self, chat_id: uuid.UUID) -> ChatTurn:
        state = self.engine.state
        recorder = self.recorder
        return ChatTurn(
            chat_id=chat_id,
            assistant_messages=list(recorder.messages),
            current_node_id=state.current_node_id,
            variables=dict(state.variables),
            actions=list(recorder.actions),
            ended=state.ended,
            logs=list(recorder.logs),
        )


def _start_state(start: ChatStart, active: ActiveGraph) -> EngineState:
    graph = active.graph
    if start.start_node_id is None:
        if not graph.start_nodes():
            raise AppError("INVALID_INPUT", NO_START_MESSAGE, field="start_node_id")
        return new_pathway_state(active)
    if graph.node(start.start_node_id) is None:
        raise AppError("INVALID_INPUT", UNKNOWN_START_MESSAGE, field="start_node_id")
    return EngineState(
        runner="pathway",
        pathway_id=active.pathway_id,
        pathway_version=active.version,
        current_node_id=start.start_node_id,
        visit_counts={start.start_node_id: 1},
    )


async def create_chat(deps: ChatDeps, ctx: RequestContext, start: ChatStart) -> ChatTurn:
    """Start a chat on the selected version and run its opening (the start node as entered)."""
    org = await _org_settings(deps, ctx.org_id)
    async with deps.database.tenant_session(ctx.org_id) as db_session:
        pathway, version = await load_version(db_session, ctx, start.pathway_id, start.version)
        stored = PathwayGraphJson.of(version)
        number = version.version_number
    graph = await asyncio.to_thread(load_graph, stored.nodes, stored.edges, stored.global_config)
    active = ActiveGraph(pathway.id, number, graph)
    state = _start_state(start, active)
    chat_id = new_id()
    session = ChatSession(
        org_id=ctx.org_id,
        pathway_id=pathway.id,
        version=number,
        graph=dict(dump_graph(graph)),
        request_data=start.request_data,
        simulate_from=start.simulate_from,
        simulate_to=start.simulate_to,
        created_at=deps.clock.now(),
        state={},
        snapshots={},
    )
    conversation = _Conversation(
        deps,
        chat_id=chat_id,
        session=session,
        graph=graph,
        state=state,
        org=org,
        counter=await asyncio.to_thread(default_counter),
    )
    await conversation.run(chat_id, conversation.engine.start())
    await _persist(ChatStore(deps.redis), chat_id, session, conversation.engine.state)
    _log.info("pathway_chat.created", chat_id=str(chat_id), pathway_id=str(pathway.id), version=number)
    return conversation.turn(chat_id)


async def _persist(store: ChatStore, chat_id: uuid.UUID, session: ChatSession, state: EngineState) -> None:
    document = _state_document(state)
    snapshots = prune_snapshots({**session.snapshots, str(state.turn_index): document})
    await store.save(chat_id, session.model_copy(update={"state": document, "snapshots": snapshots}))


def _branch(session: ChatSession, turn: int) -> ChatSession:
    snapshot = session.snapshots.get(str(turn))
    if snapshot is None:
        raise AppError("INVALID_INPUT", NO_SNAPSHOT_MESSAGE, field="branch_from_turn")
    kept = {key: value for key, value in session.snapshots.items() if int(key) <= turn}
    return session.model_copy(update={"state": snapshot, "snapshots": kept})


async def send_message(
    deps: ChatDeps, ctx: RequestContext, chat_id: uuid.UUID, message: str, branch_from_turn: int | None
) -> ChatTurn:
    """Answer ``message`` in the chat, or (``branch_from_turn``) in a new chat forked at that turn."""
    store = ChatStore(deps.redis)
    async with store.lock(chat_id):
        session = await store.load(ctx.org_id, chat_id)
        target = chat_id
        if branch_from_turn is not None:
            session = _branch(session, branch_from_turn)
            target = new_id()
        state = load_engine_state(session.state)
        if state.ended:
            raise AppError("CONFLICT", ENDED_MESSAGE)
        org = await _org_settings(deps, ctx.org_id)
        graph = await asyncio.to_thread(_graph_of, session)
        conversation = _Conversation(
            deps,
            chat_id=target,
            session=session,
            graph=graph,
            state=state,
            org=org,
            counter=await asyncio.to_thread(default_counter),
        )
        elapsed_ms = max((deps.clock.now() - session.created_at).total_seconds() * 1000, 0.0)
        turn = UserTurn(index=state.turn_index + 1, text=message, start_ms=elapsed_ms, end_ms=elapsed_ms)
        await conversation.run(target, conversation.engine.handle_user_turn(turn))
        await _persist(store, target, session, conversation.engine.state)
    if target != chat_id:
        _log.info("pathway_chat.branched", chat_id=str(chat_id), branch_id=str(target), turn=branch_from_turn)
    return conversation.turn(target)


async def delete_chat(deps: ChatDeps, ctx: RequestContext, chat_id: uuid.UUID) -> None:
    """Delete the chat (its branches are separate chats and stay)."""
    await ChatStore(deps.redis).delete(ctx.org_id, chat_id)
