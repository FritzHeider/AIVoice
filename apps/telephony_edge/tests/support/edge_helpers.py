"""Telephony edge test helpers: signing keys, signed envelopes, apps over a real or unreachable database, and
a recording carrier signaller."""

from __future__ import annotations

import asyncio
import base64
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import orjson
from fakeredis import FakeAsyncRedis
from nacl.signing import SigningKey
from service_helpers import UNREACHABLE, FakeContainer, make_client, settings_of
from temporalio.client import Client
from temporalio.converter import DataConverter

from va_common.checks import TemporalConnector
from va_common.health import CheckFn
from va_common.ids import new_id
from va_common.metrics import Service, build_metrics
from va_common.service import StopController
from va_common.settings import DatabaseSettings
from va_common.time import Clock, SystemClock
from va_db.models import TelnyxEvent
from va_db.repositories import NewTelnyxEvent, TelnyxEventRepo
from va_db.session import Database
from va_domain.carrier import CarrierEvent
from va_edge.container import EdgeDependencies
from va_edge.main import create_app
from va_edge.processor import EventHandler, EventProcessor, StoredEvent
from va_edge.settings import EdgeSettings
from va_telnyx.client_state import ClientState, encode
from va_telnyx.webhooks import parse_envelope

__all__ = [
    "DbContainer",
    "Keypair",
    "ProbeContainer",
    "RecordingSignaller",
    "client_state",
    "edge_client",
    "edge_settings",
    "envelope",
    "event_id_of",
    "event_row",
    "make_processor",
    "metric",
    "never_temporal",
    "post",
    "running",
    "signed_headers",
    "stored_event",
    "unreachable_database",
]


@dataclass(frozen=True, slots=True)
class Keypair:
    signing: SigningKey

    @classmethod
    def generate(cls) -> Keypair:
        return cls(SigningKey.generate())

    @property
    def public_b64(self) -> str:
        return base64.b64encode(bytes(self.signing.verify_key)).decode("ascii")


def signed_headers(body: bytes, key: Keypair, *, timestamp: int | None = None) -> dict[str, str]:
    ts = str(int(time.time()) if timestamp is None else timestamp)
    signature = key.signing.sign(f"{ts}|".encode() + body).signature
    return {
        "telnyx-signature-ed25519": base64.b64encode(signature).decode("ascii"),
        "telnyx-timestamp": ts,
        "content-type": "application/json",
    }


def client_state(org_id: uuid.UUID, call_id: uuid.UUID | None = None) -> str:
    return encode(ClientState(call_id=call_id or new_id(), leg="primary", org_id=org_id))


def envelope(
    event_type: str = "call.answered",
    *,
    event_id: str | None = None,
    payload: Mapping[str, Any] | None = None,
) -> bytes:
    """A Telnyx event envelope; voice events get a ``call_control_id`` unless ``payload`` is given."""
    body = {
        "data": {
            "record_type": "event",
            "id": event_id or str(uuid.uuid4()),
            "event_type": event_type,
            "occurred_at": datetime.now(UTC).isoformat(),
            "payload": dict(payload)
            if payload is not None
            else {"call_control_id": f"v3:{uuid.uuid4().hex}"},
        },
        "meta": {"attempt": 1, "delivered_to": "https://edge.test/telnyx/voice"},
    }
    return orjson.dumps(body)


def event_id_of(body: bytes) -> str:
    """The ``data.id`` (``telnyx_event_id``) of an envelope built by :func:`envelope`."""
    event_id: str = orjson.loads(body)["data"]["id"]
    return event_id


def edge_settings(public_key: str, **values: Any) -> EdgeSettings:
    return settings_of(EdgeSettings, telnyx_public_key=public_key, **values)


async def _no_temporal(host: str, _namespace: str, _converter: DataConverter | None) -> Client:
    msg = f"tests never connect to Temporal ({host})"
    raise ConnectionError(msg)


def never_temporal() -> TemporalConnector:
    """A connector whose first use raises ``ConnectionError`` (no test talks to Temporal)."""
    return TemporalConnector("temporal.test:7233", "default", connect=_no_temporal)


@dataclass
class RecordingSignaller:
    """A ``CarrierSignaller`` that records every signal; ``error`` is raised (after recording) when set."""

    signals: list[CarrierEvent] = field(default_factory=list)
    error: Exception | None = None

    async def signal(self, event: CarrierEvent) -> None:
        self.signals.append(event)
        if self.error is not None:
            raise self.error


@dataclass
class DbContainer:
    """An edge container over a real database (no readiness dependencies), fakeredis, and no Temporal."""

    url: str
    database: Database = field(init=False)
    redis: FakeAsyncRedis = field(default_factory=FakeAsyncRedis)
    temporal: TemporalConnector = field(default_factory=never_temporal)

    def readiness_checks(self) -> dict[str, CheckFn]:
        return {}

    @asynccontextmanager
    async def __call__(self, _settings: object) -> AsyncIterator[EdgeDependencies]:
        settings = settings_of(DatabaseSettings, database_url=self.url, database_pool_size=4)
        self.database = Database.from_settings(settings)
        try:
            yield self
        finally:
            await self.database.dispose()


def unreachable_database() -> Database:
    return Database.from_settings(settings_of(DatabaseSettings, **UNREACHABLE))


@dataclass
class ProbeContainer(FakeContainer):
    """A probe-test container whose database is never reachable (no connection is opened unless used)."""

    database: Database = field(default_factory=unreachable_database)
    redis: FakeAsyncRedis = field(default_factory=FakeAsyncRedis)
    temporal: TemporalConnector = field(default_factory=never_temporal)


@asynccontextmanager
async def edge_client(
    url: str,
    key: Keypair,
    *,
    handlers: Mapping[str, EventHandler] | None = None,
    clock: Clock | None = None,
    stop: StopController | None = None,
) -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    """A running edge app on database ``url`` that trusts ``key`` as the platform key: ``(client, app)``."""
    app = create_app(
        edge_settings(key.public_b64),
        container_factory=DbContainer(url),
        init_observability=False,
        handlers=handlers,
        clock=clock if clock is not None else SystemClock(),
        stop=stop,
    )
    async with make_client(app) as client:
        yield client, app


async def post(
    client: httpx.AsyncClient, path: str, body: bytes, key: Keypair, *, timestamp: int | None = None
) -> httpx.Response:
    return await client.post(path, content=body, headers=signed_headers(body, key, timestamp=timestamp))


def metric(app: Any, event_type: str, result: str) -> float:
    value = app.state.metrics.registry.get_sample_value(
        "va_telnyx_webhooks_total", {"event_type": event_type, "result": result}
    )
    return float(value or 0.0)


async def stored_event(database: Database, body: bytes | None = None, **values: Any) -> StoredEvent:
    """Insert the envelope ``body`` (default: a ``call.answered``) as a ``telnyx_events`` row."""
    raw = body if body is not None else envelope()
    parsed = parse_envelope(raw)
    event = NewTelnyxEvent(
        telnyx_event_id=parsed.data.id,
        event_type=parsed.data.event_type,
        occurred_at=parsed.data.occurred_at,
        payload=orjson.loads(raw),
        call_control_id=parsed.data.payload.get("call_control_id"),
        **values,
    )
    async with database.system_session() as session:
        row = await TelnyxEventRepo(session).record(event)
        assert row is not None
        return StoredEvent.from_row(row)


async def event_row(database: Database, event_id: uuid.UUID) -> TelnyxEvent:
    async with database.system_session() as session:
        return await session.get_one(TelnyxEvent, event_id)


def make_processor(
    database: Database,
    handlers: Mapping[str, EventHandler] | None = None,
    *,
    clock: Clock | None = None,
    **knobs: int,
) -> EventProcessor:
    """An :class:`EventProcessor` with a fresh metrics registry (wall clock unless ``clock`` is given)."""
    return EventProcessor(
        database,
        clock=clock if clock is not None else SystemClock(),
        metrics=build_metrics(Service.TELEPHONY_EDGE),
        handlers=handlers or {},
        **knobs,
    )


@asynccontextmanager
async def running(processor: EventProcessor) -> AsyncIterator[None]:
    """Run the processor's shard workers for the duration of the block."""
    task = asyncio.create_task(processor.run())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@asynccontextmanager
async def probe_edge(key: Keypair) -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    """An edge app trusting ``key`` on a database that is never reachable: ``(client, app)``."""
    app = create_app(
        edge_settings(key.public_b64), container_factory=ProbeContainer(), init_observability=False
    )
    async with make_client(app) as client:
        yield client, app
