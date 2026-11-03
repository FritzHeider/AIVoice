"""Inbound routing rig: Postgres rows, FakeTelnyx (in-process, accelerated clock, no first-command timeout),
and an :class:`InboundRouter` whose Telnyx commands are recorded.

FakeTelnyx's number inventory is fixed while ``phone_numbers.e164`` is unique across the shared test database,
so :meth:`RoutingRig.number` inserts a random number into the database and assigns the same number to a sim
account directly (an owned number record on the account's Call Control App), bypassing number orders.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Final

import httpx
import orjson
from billing_rows import new_billing_org
from edge_helpers import event_row, make_processor, running, stored_event
from fakeredis import FakeAsyncRedis
from sms_rows import add_telnyx_account
from starlette.applications import Starlette
from telnyx_helpers import APP_URL, Received, RecordingTransport, create_app, settle, telnyx_state

from va_billing.limits import CallLimiter
from va_common.ids import new_id
from va_common.metrics import Service, build_metrics
from va_common.time import SystemClock
from va_db.models import PhoneNumber, TelnyxEvent
from va_db.repositories import OrgRepo
from va_db.session import Database
from va_edge.accept import INBOUND_HANDLER_BUDGET_S
from va_edge.processor import StoredEvent
from va_edge.routing import InboundAcceptor, InboundCall, InboundRouter
from va_sim.server import simulator_routes
from va_sim.state import SimState
from va_sim.telnyx.store import DEFAULT_API_KEY
from va_telnyx import TelnyxClientFactory, TelnyxRateLimiter
from va_telnyx.factory import KeyResolver
from va_telnyx.retry import RetryPolicy
from va_telnyx.voice_events import CallInitiated
from va_telnyx.webhooks import EventPayload

__all__ = [
    "CALLER",
    "FALLBACK",
    "Command",
    "RecordingAcceptor",
    "RoutingRig",
    "failing_telnyx",
    "inbound_handler",
    "own_number",
    "random_number",
    "routing_rig",
]

CALLER: Final = "+14155550123"
FALLBACK: Final = "+14155550188"
SIM_BASE_URL: Final = "http://sim/v2"
SIM_HEADERS: Final = {"authorization": f"Bearer {DEFAULT_API_KEY}"}


def random_number() -> str:
    """A fresh 628 number (San Francisco; distinct from the other edge tests' numbers)."""
    return f"+1628{secrets.randbelow(10**7):07d}"


async def own_number(  # noqa: PLR0913 - keyword-only account parts
    database: Database,
    state: SimState,
    org_id: uuid.UUID,
    inbound_config: dict[str, Any] | None,
    *,
    connection_id: str,
    account: tuple[uuid.UUID | None, str] = (None, DEFAULT_API_KEY),
) -> str:
    """A fresh active number of the org with ``inbound_config`` (``account``: its Telnyx account id, ``None``
    for the platform, and FakeTelnyx API key), owned in FakeTelnyx on the Call Control App
    ``connection_id``."""
    e164 = random_number()
    account_id, key = account
    async with database.tenant_session(org_id) as session:
        session.add(
            PhoneNumber(
                id=new_id(),
                org_id=org_id,
                e164=e164,
                country_code="US",
                status="active",
                telnyx_account_id=account_id,
                inbound_config=inbound_config or {},
            )
        )
    record_id = state.ids.uuid()
    state.telnyx.accounts[key].numbers[record_id] = {
        "id": record_id,
        "record_type": "phone_number",
        "phone_number": e164,
        "connection_id": connection_id,
        "messaging_profile_id": None,
    }
    return e164


@dataclass
class RecordingAcceptor:
    """An :class:`InboundAcceptor` that records accepted calls; ``error`` is raised after recording."""

    calls: list[InboundCall] = field(default_factory=list)
    error: Exception | None = None

    async def accept(self, call: InboundCall) -> None:
        self.calls.append(call)
        if self.error is not None:
            raise self.error


@dataclass(frozen=True, slots=True)
class Command:
    """A Call Control command FakeTelnyx received: action name, JSON body, ``Authorization`` header."""

    action: str
    body: dict[str, Any]
    authorization: str


def inbound_handler(router: InboundRouter) -> Any:
    """A processor handler that routes incoming ``call.initiated`` events only (like the carrier handler)."""

    async def handle(event: StoredEvent, payload: EventPayload) -> None:
        if isinstance(payload, CallInitiated) and payload.direction == "incoming":
            await router.route(event, payload)

    return handle


@dataclass
class RoutingRig:
    database: Database
    state: SimState
    transport: RecordingTransport
    raw: httpx.AsyncClient
    factory: TelnyxClientFactory
    redis: FakeAsyncRedis
    limiter: CallLimiter
    router: InboundRouter
    acceptor: RecordingAcceptor
    app_ids: dict[str, str]
    sent: list[httpx.Request]
    byo_keys: dict[uuid.UUID, str]
    metrics: Any

    async def org(self, *, status: str = "active", balance: int = 1_000_000, **values: Any) -> uuid.UUID:
        """A funded org on the build plan (created 30 days ago); ``status`` other than active is set after."""
        org_id = await new_billing_org(self.database, balance=balance, **values)
        if status != "active":
            async with self.database.system_session() as session:
                await OrgRepo(session).set_status(org_id, status)  # type: ignore[arg-type]
        return org_id

    async def byo_account(self, org_id: uuid.UUID) -> tuple[uuid.UUID, str]:
        """A BYO Telnyx account of the org (DB row + sim account with its own Call Control App)."""
        account_id = await add_telnyx_account(self.database, org_id, None)
        key = f"byo-{secrets.token_hex(8)}"
        self.byo_keys[account_id] = key
        response = await self.raw.post("/sim/telnyx/accounts", json={"api_key": key})
        assert response.status_code < 300, response.text
        app = await self.raw.post(
            "/v2/call_control_applications",
            json={"application_name": "byo", "webhook_event_url": APP_URL, "first_command_timeout": False},
            headers={"authorization": f"Bearer {key}"},
        )
        assert app.status_code == 201, app.text
        self.app_ids[key] = app.json()["data"]["id"]
        return account_id, key

    async def number(
        self,
        org_id: uuid.UUID,
        inbound_config: dict[str, Any] | None = None,
        *,
        account: tuple[uuid.UUID, str] | None = None,
    ) -> str:
        """An active number of the org with ``inbound_config``, also owned in FakeTelnyx (platform or BYO)."""
        account_id, key = account if account is not None else (None, DEFAULT_API_KEY)
        return await own_number(
            self.database,
            self.state,
            org_id,
            inbound_config,
            connection_id=self.app_ids[key],
            account=(account_id, key),
        )

    async def place(
        self, to: str, caller: str = CALLER, *, time_limit_s: float | None = None
    ) -> tuple[str, Received]:
        """An incoming call to ``to``: its call_control_id and FakeTelnyx's ``call.initiated`` webhook.

        ``time_limit_s`` shortens the sim leg's time limit (default 4 h). The accelerated clock jumps to that
        limit whenever the loop waits on Postgres, and a recorded leg then encodes a WAV of the whole virtual
        duration (~460 MB for 4 h dual-channel), enough to get an xdist worker OOM-killed."""
        response = await self.raw.post("/sim/telnyx/inbound", json={"to": to, "from": caller})
        assert response.status_code == 202, response.text
        ccid: str = response.json()["call_control_id"]
        if time_limit_s is not None:
            accounts = self.state.telnyx.accounts.values()
            [leg] = [account.calls[ccid] for account in accounts if ccid in account.calls]
            leg.time_limit_s = time_limit_s
        await settle(self.state)
        [initiated] = [
            item
            for item in self.transport.of_type("call.initiated")
            if item.envelope["data"]["payload"]["call_control_id"] == ccid
        ]
        return ccid, initiated

    async def ring(
        self, to: str, caller: str = CALLER, *, time_limit_s: float | None = None
    ) -> tuple[str, StoredEvent]:
        """:meth:`place`, with the webhook stored as a ``telnyx_events`` row."""
        ccid, initiated = await self.place(to, caller, time_limit_s=time_limit_s)
        return ccid, await stored_event(self.database, initiated.body)

    def router_on(
        self, telnyx: TelnyxClientFactory, *, acceptor: InboundAcceptor | None = None
    ) -> InboundRouter:
        """Another router of this rig sending its commands through ``telnyx`` (accepting with ``acceptor``,
        default the recording acceptor)."""
        return InboundRouter(
            self.database,
            self.limiter,
            telnyx,
            clock=SystemClock(),
            metrics=self.metrics,
            acceptor=acceptor if acceptor is not None else self.acceptor,
        )

    async def stored(self, event: StoredEvent) -> StoredEvent:
        """``event`` as stored now (its attempts counted)."""
        return StoredEvent.from_row(await event_row(self.database, event.id))

    async def route(self, event: StoredEvent, *, router: InboundRouter | None = None) -> TelnyxEvent:
        """Process ``event`` through an :class:`EventProcessor` running the router; the stored row after."""
        handler = inbound_handler(router if router is not None else self.router)
        processor = make_processor(
            self.database,
            {"call.initiated": handler},
            budgets={"call.initiated": INBOUND_HANDLER_BUDGET_S},
        )
        async with running(processor):
            assert processor.offer(event)
            await processor.join()
        await settle(self.state)
        return await event_row(self.database, event.id)

    def commands(self, ccid: str) -> list[Command]:
        """The Call Control commands sent for ``ccid``, in order."""
        prefix = f"/v2/calls/{ccid}/actions/"
        return [
            Command(
                request.url.path.removeprefix(prefix),
                orjson.loads(request.content),
                request.headers.get("authorization", ""),
            )
            for request in self.sent
            if request.url.path.startswith(prefix)
        ]

    async def leg(self, ccid: str) -> dict[str, Any]:
        response = await self.raw.get(f"/sim/telnyx/calls/{ccid}")
        assert response.status_code == 200, response.text
        found: dict[str, Any] = response.json()
        return found

    def counted(self, result: str) -> float:
        value = self.metrics.registry.get_sample_value("va_inbound_calls_total", {"result": result})
        return float(value or 0.0)


class _ServerErrorTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503, json={"errors": [{"code": "90000", "title": "Service unavailable"}]}, request=request
        )


async def _no_key(_account_id: uuid.UUID) -> str | None:
    return None


def sim_factory(
    http: httpx.AsyncClient, redis: FakeAsyncRedis, resolve_key: KeyResolver
) -> TelnyxClientFactory:
    """A factory on FakeTelnyx's platform key over ``http`` (no client retries)."""
    limiter = TelnyxRateLimiter(redis, clock=SystemClock())
    no_retries = RetryPolicy(delays_s=())
    return TelnyxClientFactory(
        base_url=SIM_BASE_URL,
        http=http,
        limiter=limiter,
        platform_api_key=DEFAULT_API_KEY,
        resolve_key=resolve_key,
        retry=no_retries,
    )


def failing_telnyx(redis: FakeAsyncRedis) -> TelnyxClientFactory:
    """A factory whose every Telnyx request answers ``503`` (no client retries)."""
    return sim_factory(httpx.AsyncClient(transport=_ServerErrorTransport()), redis, _no_key)


@asynccontextmanager
async def routing_rig(
    database: Database, *, acceptor: InboundAcceptor | None = None, with_acceptor: bool = True
) -> AsyncIterator[RoutingRig]:
    """The rig; ``with_acceptor=False`` builds the router without an acceptor (``accept_unavailable``)."""
    transport = RecordingTransport()
    state = telnyx_state(transport, seed=secrets.randbits(32))
    asgi = httpx.ASGITransport(app=Starlette(routes=simulator_routes("telnyx", state)))
    redis = FakeAsyncRedis()
    sent: list[httpx.Request] = []
    byo_keys: dict[uuid.UUID, str] = {}

    async def record(request: httpx.Request) -> None:
        sent.append(request)

    async def resolve_key(account_id: uuid.UUID) -> str | None:
        return byo_keys.get(account_id)

    clock = SystemClock()
    recording = RecordingAcceptor()
    chosen = acceptor if acceptor is not None else recording
    metrics = build_metrics(Service.TELEPHONY_EDGE)
    try:
        async with AsyncExitStack() as clients:
            raw = await clients.enter_async_context(
                httpx.AsyncClient(transport=asgi, base_url="http://sim", headers=SIM_HEADERS)
            )
            http = await clients.enter_async_context(
                httpx.AsyncClient(transport=asgi, event_hooks={"request": [record]})
            )
            factory = sim_factory(http, redis, resolve_key)
            limiter = CallLimiter(redis, clock=clock)
            router = InboundRouter(
                database,
                limiter,
                factory,
                clock=clock,
                metrics=metrics,
                acceptor=chosen if with_acceptor else None,
            )
            app = await create_app(raw, first_command_timeout=False)
            yield RoutingRig(
                database=database,
                state=state,
                transport=transport,
                raw=raw,
                factory=factory,
                redis=redis,
                limiter=limiter,
                router=router,
                acceptor=recording,
                app_ids={DEFAULT_API_KEY: app["id"]},
                sent=sent,
                byo_keys=byo_keys,
                metrics=metrics,
            )
    finally:
        await state.aclose()
        await redis.aclose()
