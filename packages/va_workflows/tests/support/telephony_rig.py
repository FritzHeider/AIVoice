"""Telephony activity rig: FakeTelnyx in process, fakeredis, the migrated test database, a real-time clock.

Real time because ``call_events`` partitions exist only around the migration month. Every dial request the
activities send is recorded (``Rig.dials``) so tests compare the exact body.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

import httpx
import orjson
from billing_rows import new_billing_org
from call_rows import SeededCall, seed_call
from fakeredis import FakeAsyncRedis, FakeServer
from resolved_configs import resolved_config
from sim_helpers import LATENCIES
from sqlalchemy import select
from starlette.applications import Starlette
from telnyx_helpers import RecordingTransport, register, telnyx_state

from va_billing.limits import CallLimiter
from va_common.time import Clock, SystemClock
from va_db.models import CallEvent, CallStatus, TelnyxAccount
from va_db.session import Database
from va_events import EventPublisher
from va_sim.clock import RealtimeClock
from va_sim.server import simulator_routes
from va_sim.settings import WebhookDefaults
from va_sim.state import SimState
from va_sim.telnyx.store import DEFAULT_API_KEY
from va_telnyx import TelnyxClientFactory, TelnyxRateLimiter
from va_telnyx.retry import RetryPolicy
from va_workflows.activities.call_end import CallEndActivities
from va_workflows.activities.dial_request import DialSettings
from va_workflows.activities.telephony import TelephonyActivities

__all__ = [
    "CALLEE",
    "CALLER",
    "MEDIA_HOST",
    "Handler",
    "Rig",
    "broken_redis",
    "call_event_rows",
    "expected_dial_body",
    "failing_factory",
    "funded_org",
    "rig",
]

SIM_BASE_URL: Final = "http://sim/v2"
VOICE_URL: Final = "https://edge.test/telnyx/voice"
MEDIA_HOST: Final = "media.test:8200"
CALLEE: Final = "+14155550101"
CALLER: Final = "+14155550190"
FUNDS_MICROS: Final = 50_000_000

type Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


@dataclass(slots=True)
class Rig:
    database: Database
    redis: FakeAsyncRedis
    state: SimState
    raw: httpx.AsyncClient
    factory: TelnyxClientFactory
    limiter: CallLimiter
    publisher: EventPublisher
    settings: DialSettings
    clock: Clock
    app_id: str
    sent: list[httpx.Request] = field(default_factory=list)
    byo_keys: dict[uuid.UUID, str] = field(default_factory=dict)
    dialed: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when the first ``POST /calls`` goes out."""

    def telephony(
        self,
        *,
        telnyx: TelnyxClientFactory | None = None,
        limiter: CallLimiter | None = None,
        publisher: EventPublisher | None = None,
    ) -> TelephonyActivities:
        return TelephonyActivities(
            database=self.database,
            telnyx=telnyx or self.factory,
            limiter=limiter or self.limiter,
            publisher=publisher or self.publisher,
            settings=self.settings,
            clock=self.clock,
        )

    def call_end(self, *, redis: FakeAsyncRedis | None = None) -> CallEndActivities:
        return CallEndActivities(
            database=self.database,
            limiter=self.limiter,
            publisher=self.publisher,
            redis=redis or self.redis,
            clock=self.clock,
        )

    def dials(self) -> list[dict[str, Any]]:
        """Bodies of every ``POST /calls`` sent through the factory's clients."""
        return [
            orjson.loads(r.content) for r in self.sent if r.method == "POST" and r.url.path == "/v2/calls"
        ]

    def commands(self, action: str) -> list[httpx.Request]:
        return [r for r in self.sent if r.url.path.endswith(f"/actions/{action}")]

    async def byo_account(
        self, org_id: uuid.UUID, key: str | None, *, app_id: str | None = None
    ) -> uuid.UUID:
        """A BYO ``telnyx_accounts`` row of ``org_id``; ``key`` (when given) is what the resolver returns."""
        account = TelnyxAccount(
            org_id=org_id,
            label="byo",
            api_key_enc=b"\x00",
            public_key="cHVibGlj",
            call_control_app_id=app_id,
            encrypted_key_handle=f"ek_{secrets.token_hex(8)}",
            status="active",
        )
        async with self.database.tenant_session(org_id) as session:
            session.add(account)
            await session.flush()
            account_id = account.id
        if key is not None:
            self.byo_keys[account_id] = key
        return account_id

    async def seed(
        self,
        status: CallStatus,
        *,
        org_id: uuid.UUID | None = None,
        config: dict[str, Any] | None = None,
        **values: Any,
    ) -> SeededCall:
        """An outbound call to :data:`CALLEE` of a funded org (fresh unless ``org_id``)."""
        org = org_id if org_id is not None else await funded_org(self.database)
        return await seed_call(
            self.database,
            status=status,
            org_id=org,
            config=config if config is not None else resolved_config("01_task_basic.json"),
            to_number=CALLEE,
            from_number=CALLER,
            **values,
        )


PREMIUM_AMD: Final = {
    "total_analysis_time_millis": 5000,
    "after_greeting_silence_millis": 800,
    "greeting_duration_millis": 3500,
    "initial_silence_millis": 3500,
    "maximum_number_of_words": 5,
    "silence_threshold": 256,
    "greeting_total_analysis_time_millis": 8000,
    "greeting_silence_duration_millis": 1500,
}
"""The specs/06 §6 step 5 premium AMD tuning, spelled out independently of the code."""


def expected_dial_body(call_id: uuid.UUID, *, connection_id: str, to: str, from_: str) -> dict[str, Any]:
    """The dial body of ``01_task_basic.json`` (recorded, PCMU, premium AMD, 5 min) without ``stream_url``
    and ``client_state`` (both carry per-call values the tests check apart)."""
    return {
        "connection_id": connection_id,
        "to": to,
        "from": from_,
        "timeout_secs": 45,
        "time_limit_secs": 330,
        "answering_machine_detection": "premium",
        "answering_machine_detection_config": PREMIUM_AMD,
        "command_id": f"dial-{call_id}",
        "webhook_url_method": "POST",
        "stream_track": "inbound_track",
        "stream_codec": "PCMU",
        "stream_bidirectional_mode": "rtp",
        "stream_bidirectional_codec": "PCMU",
        "stream_bidirectional_sampling_rate": 8000,
        "stream_bidirectional_target_legs": "self",
        "record": "record-from-answer",
        "record_channels": "dual",
        "record_format": "wav",
        "record_track": "both",
        "custom_headers": [{"name": "X-VA-Call-Id", "value": str(call_id)}],
        "send_silence_when_idle": True,
    }


async def funded_org(database: Database, **values: Any) -> uuid.UUID:
    """An org on the ``build`` plan with $50, created 30 days ago (so it is no "new account")."""
    created = datetime.now().astimezone() - timedelta(days=30)
    return await new_billing_org(database, **{"balance": FUNDS_MICROS, "created_at": created, **values})


async def call_event_rows(database: Database, call: SeededCall) -> list[CallEvent]:
    async with database.tenant_session(call.org_id) as session:
        rows = await session.scalars(
            select(CallEvent).where(CallEvent.call_id == call.call_id).order_by(CallEvent.seq)
        )
        return list(rows.all())


def broken_redis() -> FakeAsyncRedis:
    """A client whose server is down (every command raises ``ConnectionError``)."""
    server = FakeServer()
    server.connected = False
    return FakeAsyncRedis(server=server)


class _Transport(httpx.AsyncBaseTransport):
    def __init__(self, handler: Handler) -> None:
        self.handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self.handler(request)


def failing_factory(r: Rig, handler: Handler) -> TelnyxClientFactory:
    """A factory whose clients answer through ``handler``, without client-side retries."""

    async def no_key(_account: uuid.UUID) -> str | None:
        return None

    return TelnyxClientFactory(
        base_url=SIM_BASE_URL,
        http=httpx.AsyncClient(transport=_Transport(handler)),
        limiter=TelnyxRateLimiter(r.redis, clock=r.clock),
        platform_api_key="KEY-failing",
        resolve_key=no_key,
        retry=RetryPolicy(delays_s=()),
    )


def _sim_state(transport: RecordingTransport, *, realtime: bool) -> SimState:
    if not realtime:
        return telnyx_state(transport, seed=secrets.randbits(32))
    return SimState(
        clock=RealtimeClock(),
        latencies=LATENCIES,
        seed=secrets.randbits(32),
        webhook_defaults=WebhookDefaults(voice_url=VOICE_URL, messaging_url=VOICE_URL),
        webhook_transport=transport,
    )


@asynccontextmanager
async def rig(
    database: Database, *, answer: str = "{result: no_answer}", realtime: bool = False
) -> AsyncIterator[Rig]:
    """The rig; FakeTelnyx's :data:`CALLEE` answers per ``answer`` (a scenario ``answer`` section), so a
    dialled call never opens its media stream unless a test asks for it. ``realtime`` runs FakeTelnyx on the
    wall clock (the accelerated clock jumps to the next carrier timer, e.g. the time limit, whenever the loop
    waits on the database)."""
    transport = RecordingTransport()
    state = _sim_state(transport, realtime=realtime)
    register(state, f'id: far-end\nmatch: {{to: "{CALLEE}"}}\nanswer: {answer}\n')
    app = Starlette(routes=simulator_routes("telnyx", state))
    asgi = httpx.ASGITransport(app=app)
    redis = FakeAsyncRedis()
    sent: list[httpx.Request] = []
    byo_keys: dict[uuid.UUID, str] = {}
    dialed = asyncio.Event()

    async def record(request: httpx.Request) -> None:
        sent.append(request)
        if request.method == "POST" and request.url.path == "/v2/calls":
            dialed.set()

    async def resolve_key(account_id: uuid.UUID) -> str | None:
        return byo_keys.get(account_id)

    headers = {"authorization": f"Bearer {DEFAULT_API_KEY}"}
    clock = SystemClock()
    try:
        async with (
            httpx.AsyncClient(transport=asgi, base_url="http://sim", headers=headers) as raw,
            httpx.AsyncClient(transport=asgi, event_hooks={"request": [record]}) as http,
        ):
            factory = TelnyxClientFactory(
                base_url=SIM_BASE_URL,
                http=http,
                limiter=TelnyxRateLimiter(redis, clock=clock),
                platform_api_key=DEFAULT_API_KEY,
                resolve_key=resolve_key,
            )
            app_id = await _provision(factory)
            sent.clear()
            yield Rig(
                database=database,
                redis=redis,
                state=state,
                raw=raw,
                factory=factory,
                limiter=CallLimiter(redis, clock=clock),
                publisher=EventPublisher(redis, call_events=None),
                settings=DialSettings.for_env(
                    "test",
                    call_control_app_id=app_id,
                    media_public_host=MEDIA_HOST,
                    stream_secret=secrets.token_bytes(32),
                    allowed_countries=frozenset({"US", "CA"}),
                ),
                clock=clock,
                app_id=app_id,
                sent=sent,
                byo_keys=byo_keys,
                dialed=dialed,
            )
    finally:
        await state.aclose()
        await redis.aclose()


async def _provision(factory: TelnyxClientFactory) -> str:
    client = factory.platform
    profile = await client.create_outbound_voice_profile("voiceagents-test")
    app = await client.create_call_control_app(
        "voiceagents-test", VOICE_URL, outbound_voice_profile_id=profile.id
    )
    return app.id
