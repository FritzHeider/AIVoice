"""Inbound accept rig: the routing rig (Postgres, FakeTelnyx, fakeredis) with a real
:class:`~va_edge.accept.InboundAccept` whose workflow starts are recorded.

The acceptor answers through its own Telnyx factory (by default the rig's), so a test can fail only the
``answer`` command while the router's refusal still reaches FakeTelnyx.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Final

import httpx
from fakeredis import FakeAsyncRedis, FakeServer
from routing_helpers import RoutingRig, routing_rig, sim_factory
from sqlalchemy import select

from va_common.metrics import Service, build_metrics
from va_common.time import Clock, SystemClock
from va_db.call_context import CallContextStore
from va_db.models import Call, CallEvent, CallLeg, Contact, TelnyxEvent, Voice
from va_db.session import Database
from va_domain.call_resolution import PlatformModels
from va_domain.call_workflow import CallWorkflowInput
from va_domain.org import OrgSettings
from va_edge.accept import AcceptSettings, InboundAccept
from va_edge.processor import StoredEvent
from va_edge.routing import InboundRouter
from va_events import DbWebhookRouter, EventPublisher
from va_telnyx import TelnyxClientFactory

__all__ = [
    "MEDIA_HOST",
    "AcceptRig",
    "GatedClock",
    "RecordingStarter",
    "accept_rig",
    "add_default_voice",
    "answer_failing_factory",
    "broken_redis",
]

MEDIA_HOST: Final = "127.0.0.1:9"
"""A media host nothing listens on: FakeTelnyx's stream connection is refused at once."""


@dataclass
class RecordingStarter:
    """An ``InboundStarter`` recording starts and terminations; ``error`` is raised after recording a start,
    ``hang`` makes the start wait forever (``entered`` is set once a start arrived)."""

    started: list[CallWorkflowInput] = field(default_factory=list)
    terminated: list[tuple[uuid.UUID, str]] = field(default_factory=list)
    error: Exception | None = None
    terminate_error: Exception | None = None
    hang: bool = False
    entered: asyncio.Event = field(default_factory=asyncio.Event)

    async def start(self, call: CallWorkflowInput) -> None:
        self.started.append(call)
        self.entered.set()
        if self.hang:
            await asyncio.Event().wait()
        if self.error is not None:
            raise self.error

    async def terminate(self, call_id: uuid.UUID, reason: str) -> None:
        self.terminated.append((call_id, reason))
        if self.terminate_error is not None:
            raise self.terminate_error


class GatedClock(SystemClock):
    """The wall clock whose ``sleep(s)`` returns only once :meth:`fire` was called with the same ``s``."""

    def __init__(self) -> None:
        self._gates: defaultdict[float, asyncio.Event] = defaultdict(asyncio.Event)

    async def sleep(self, seconds: float) -> None:
        await self._gates[seconds].wait()

    def fire(self, seconds: float) -> None:
        self._gates[seconds].set()


class _AnswerFailing(httpx.AsyncBaseTransport):
    """Answers every ``.../actions/answer`` with ``status`` (code ``code``) and passes everything else on."""

    def __init__(self, inner: httpx.AsyncBaseTransport, status: int, code: str) -> None:
        self._inner = inner
        self._status = status
        self._code = code

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/actions/answer"):
            body = {"errors": [{"code": self._code, "title": "Answer refused"}]}
            return httpx.Response(self._status, json=body, request=request)
        return await self._inner.handle_async_request(request)


def answer_failing_factory(rig: RoutingRig, *, status: int = 500, code: str = "90000") -> TelnyxClientFactory:
    """A factory over the rig's FakeTelnyx whose ``answer`` commands fail with ``status``/``code``."""
    inner = rig.raw._transport
    http = httpx.AsyncClient(transport=_AnswerFailing(inner, status, code))

    async def resolve_key(account_id: uuid.UUID) -> str | None:
        return rig.byo_keys.get(account_id)

    return sim_factory(http, rig.redis, resolve_key)


async def add_default_voice(
    database: Database, org_id: uuid.UUID, provider_voice_id: str | None = None
) -> None:
    """An org voice named like the default ``default_voice`` (inbound resolution needs it);
    ``provider_voice_id`` defaults to a random one."""
    voice_id = provider_voice_id if provider_voice_id is not None else secrets.token_hex(10)
    async with database.tenant_session(org_id) as session:
        session.add(
            Voice(
                id=uuid.uuid4(), org_id=org_id, name=OrgSettings().default_voice, provider_voice_id=voice_id
            )
        )


@dataclass
class AcceptRig:
    rig: RoutingRig
    starter: RecordingStarter
    accept: InboundAccept
    router: InboundRouter
    secret: bytes
    metrics: Any

    @property
    def database(self) -> Database:
        return self.rig.database

    async def org(self, **values: Any) -> uuid.UUID:
        """A funded org (the routing rig's) owning a voice named like the default ``default_voice``."""
        org_id = await self.rig.org(**values)
        await add_default_voice(self.database, org_id)
        return org_id

    async def route(self, event: StoredEvent) -> TelnyxEvent:
        """Process ``event`` through a processor running this rig's router; the stored row after."""
        return await self.rig.route(event, router=self.router)

    async def calls(self, org_id: uuid.UUID) -> list[Call]:
        async with self.database.tenant_session(org_id) as session:
            rows = await session.scalars(select(Call).where(Call.org_id == org_id).order_by(Call.created_at))
            return list(rows.all())

    async def legs(self, org_id: uuid.UUID, call_id: uuid.UUID) -> list[CallLeg]:
        async with self.database.tenant_session(org_id) as session:
            rows = await session.scalars(
                select(CallLeg).where(CallLeg.org_id == org_id, CallLeg.call_id == call_id)
            )
            return list(rows.all())

    async def contacts(self, org_id: uuid.UUID) -> list[Contact]:
        async with self.database.tenant_session(org_id) as session:
            return list((await session.scalars(select(Contact).where(Contact.org_id == org_id))).all())

    async def events(self, org_id: uuid.UUID, call_id: uuid.UUID) -> list[CallEvent]:
        async with self.database.tenant_session(org_id) as session:
            rows = await session.scalars(
                select(CallEvent)
                .where(CallEvent.org_id == org_id, CallEvent.call_id == call_id)
                .order_by(CallEvent.seq)
            )
            return list(rows.all())

    def answered(self) -> float:
        """How many ring-to-answer times were observed."""
        value = self.metrics.registry.get_sample_value("va_inbound_answer_ms_count")
        return float(value or 0.0)


@asynccontextmanager
async def accept_rig(
    database: Database,
    *,
    clock: Clock | None = None,
    telnyx: Callable[[RoutingRig], TelnyxClientFactory] | None = None,
    contexts_redis: Callable[[RoutingRig], Any] | None = None,
) -> AsyncIterator[AcceptRig]:
    """The rig; ``telnyx`` builds the acceptor's factory, ``contexts_redis`` the client the contexts go to."""
    async with routing_rig(database) as rig:
        secret = secrets.token_bytes(32)
        metrics = build_metrics(Service.TELEPHONY_EDGE)
        starter = RecordingStarter()
        redis = contexts_redis(rig) if contexts_redis is not None else rig.redis
        accept = InboundAccept(
            database,
            CallContextStore(redis),
            starter,
            telnyx(rig) if telnyx is not None else rig.factory,
            publisher=EventPublisher(rig.redis, call_events=None, router=DbWebhookRouter(database)),
            settings=AcceptSettings(
                media_public_host=MEDIA_HOST,
                media_ws_scheme="ws",
                stream_secret=secret,
                models=PlatformModels("dialogue-model", "router-model"),
                task_queue="va-main",
            ),
            clock=clock if clock is not None else SystemClock(),
            metrics=metrics,
        )
        yield AcceptRig(
            rig=rig,
            starter=starter,
            accept=accept,
            router=rig.router_on(rig.factory, acceptor=accept),
            secret=secret,
            metrics=metrics,
        )


def broken_redis() -> FakeAsyncRedis:
    """A fakeredis client whose server is down (every command raises ``ConnectionError``)."""
    server = FakeServer()
    server.connected = False
    return FakeAsyncRedis(server=server)
