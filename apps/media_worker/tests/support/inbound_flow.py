"""An inbound call end to end in one process: FakeTelnyx (wall clock) delivers its webhooks to a running
telephony edge app (routing, inbound accept, carrier events) whose answer streams the call to a served media
worker running :class:`~va_media.runtime.CallRuntime` over FakeElevenLabs and FakeLLM, with the end-of-call
routine on the media database.

The edge and the media worker share ONE fakeredis (carrier state and channels, call contexts, concurrency
slots, the org settings cache), as they share Redis in production. Temporal is a
:class:`workflow_fakes.SignalRecorder`: the edge's inbound ``CallWorkflow`` start and its carrier signals are
recorded. The media worker signals ``CallWorkflow`` through a :class:`media_helpers.RecordingSignaller`.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final

import httpx
import orjson
from accept_helpers import add_default_voice
from billing_rows import new_billing_org
from edge_helpers import DbContainer, edge_settings
from fakeredis import FakeAsyncRedis
from media_helpers import FakeMediaContainer, served_media
from routing_helpers import CALLER, own_number
from runtime_rig import MODELS_DIR, RecordingRuntime, sim_runtime
from service_helpers import make_client, settings_of
from sim_media import free_port, realtime_telnyx
from sqlalchemy import select
from starlette.applications import Starlette
from telnyx_helpers import RecordingTransport, create_app, register, telnyx_client
from tenant_rows import open_database
from tts_helpers import VOICE_ID
from workflow_fakes import SignalRecorder, connector_of

from va_common.time import SystemClock
from va_db.models import Call, CallTurn
from va_db.org_settings import OrgSettingsReader
from va_db.session import Database
from va_edge.main import create_app as create_edge_app
from va_media.loop_lag import LoopLagMonitor
from va_media.main import create_app as create_media_app
from va_media.runtime import open_persistence
from va_media.settings import MediaSettings
from va_sim.server import running_sims
from va_sim.state import SimState

__all__ = ["EdgeForwarder", "InboundFlow", "MediaDbContainer", "inbound_flow"]

BALANCE_MICROS: Final = 50_000_000


class EdgeForwarder(RecordingTransport):
    """FakeTelnyx's webhook receiver: records every webhook and posts it to the edge app (once ``edge`` is
    set), answering with the edge's status."""

    def __init__(self) -> None:
        super().__init__()
        self.edge: httpx.AsyncClient | None = None
        self.statuses: list[int] = []
        self._forwarded = asyncio.Condition()
        self._documents: list[dict[str, Any]] = []

    async def wait_for(self, predicate: Callable[[list[dict[str, Any]]], bool]) -> None:
        """Until ``predicate`` holds for the ``data`` documents of the webhooks the edge answered (woken by
        every forwarded webhook, no polling)."""
        async with self._forwarded:
            await self._forwarded.wait_for(lambda: predicate(self.forwarded()))

    def forwarded(self) -> list[dict[str, Any]]:
        return list(self._documents)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        recorded = await super().handle_async_request(request)
        if self.edge is None:
            return recorded
        item = self.received[-1]
        path = "/telnyx/messaging" if "/messaging" in item.url else "/telnyx/voice"
        forwarded = await self.edge.post(path, content=item.body, headers=dict(item.headers))
        self.statuses.append(forwarded.status_code)
        self._documents.append(orjson.loads(item.body)["data"])
        async with self._forwarded:
            self._forwarded.notify_all()
        return httpx.Response(forwarded.status_code)


@dataclass
class MediaDbContainer(FakeMediaContainer):
    """The media container with the media database (the production context loader reads it)."""

    database: Database | None = None


@dataclass
class InboundFlow:
    state: SimState
    database: Database
    llm: httpx.AsyncClient
    body: RecordingRuntime
    runtime_signals: Any
    temporal: SignalRecorder
    forwarder: EdgeForwarder
    edge_app: Any
    sim: httpx.AsyncClient
    app_id: str

    async def org(self) -> uuid.UUID:
        """A funded org owning a voice named like the default voice (FakeElevenLabs' seeded voice)."""
        org_id = await new_billing_org(self.database, balance=BALANCE_MICROS)
        await add_default_voice(self.database, org_id, VOICE_ID)
        return org_id

    async def number(self, org_id: uuid.UUID, inbound_config: dict[str, Any]) -> str:
        return await own_number(self.database, self.state, org_id, inbound_config, connection_id=self.app_id)

    def caller(self, *steps: str, transfer_target: str | None = None) -> str:
        """An inbound caller scenario (bound to :data:`CALLER`; ``transfer_target``: a YAML flow mapping for
        the legs the call is transferred to); its id."""
        text = f'id: in-{secrets.token_hex(4)}\nmatch: {{inbound_from: "{CALLER}"}}\nsteps:\n'
        text += "".join(f"  - {step}\n" for step in steps)
        if transfer_target is not None:
            text += f"transfer_target: {transfer_target}\n"
        return register(self.state, text)

    async def ring(self, to: str, scenario_id: str) -> str:
        """The caller dials ``to``; the call_control_id."""
        response = await self.sim.post(
            "/sim/telnyx/inbound", json={"to": to, "from": CALLER, "scenario": scenario_id}
        )
        assert response.status_code == 202, response.text
        ccid: str = response.json()["call_control_id"]
        return ccid

    async def call(self, org_id: uuid.UUID) -> Call:
        async with self.database.tenant_session(org_id) as session:
            [call] = (await session.scalars(select(Call).where(Call.org_id == org_id))).all()
            return call

    async def turns(self, org_id: uuid.UUID, call_id: uuid.UUID) -> list[CallTurn]:
        async with self.database.tenant_session(org_id) as session:
            rows = await session.scalars(
                select(CallTurn)
                .where(CallTurn.org_id == org_id, CallTurn.call_id == call_id)
                .order_by(CallTurn.turn_index)
            )
            return list(rows.all())

    def answer_ms(self) -> tuple[float, float]:
        """``(count, sum)`` of the edge's ``va_inbound_answer_ms`` histogram."""
        registry = self.edge_app.state.metrics.registry
        count = registry.get_sample_value("va_inbound_answer_ms_count") or 0.0
        total = registry.get_sample_value("va_inbound_answer_ms_sum") or 0.0
        return float(count), float(total)


@asynccontextmanager
async def inbound_flow(database_url: str) -> AsyncIterator[InboundFlow]:
    """The edge, the media worker and the simulators wired for inbound calls on ``database_url``."""
    forwarder = EdgeForwarder()
    state = realtime_telnyx(forwarder)
    shared = FakeAsyncRedis()
    secret = secrets.token_bytes(32)
    port = free_port()
    media_host = f"127.0.0.1:{port}"
    clock = SystemClock()
    temporal = SignalRecorder()
    async with (
        running_sims(state, ports={"elevenlabs": 0, "llm": 0}) as urls,
        open_database(database_url) as database,
        open_persistence(database, shared, clock=clock) as persistence,
        sim_runtime(
            state,
            urls,
            org_settings=OrgSettingsReader(database, shared),
            phrase_redis=shared,
            persistence=persistence,
            database=database,
            end_redis=shared,
        ) as parts,
        httpx.AsyncClient(base_url=urls["llm"]) as llm,
    ):
        edge_app = create_edge_app(
            edge_settings(
                state.signing_key.public_key_b64,
                stream_token_secret=secret.hex(),
                media_public_host=media_host,
            ),
            container_factory=DbContainer(
                database_url, redis=shared, telnyx=parts.telnyx, temporal=connector_of(temporal)
            ),
            init_observability=False,
            clock=clock,
        )
        media_app: Starlette = create_media_app(
            settings_of(
                MediaSettings,
                pod_id="pod-inbound",
                stream_token_secret=secret.hex(),
                telnyx_public_key=state.signing_key.public_key_b64,
                media_public_host=media_host,
            ),
            container_factory=MediaDbContainer(redis=shared, database=database),
            init_observability=False,
            models_dir=MODELS_DIR,
            monitor=LoopLagMonitor(interval_s=3600),
            clock=clock,
            body=parts.body,
        )
        async with (
            served_media(media_app, port=port),
            make_client(edge_app) as edge,
            telnyx_client(state) as sim,
        ):
            forwarder.edge = edge
            app = await create_app(sim, first_command_timeout=False)
            yield InboundFlow(
                state=state,
                database=database,
                llm=llm,
                body=parts.body,
                runtime_signals=parts.signaller,
                temporal=temporal,
                forwarder=forwarder,
                edge_app=edge_app,
                sim=sim,
                app_id=app["id"],
            )
