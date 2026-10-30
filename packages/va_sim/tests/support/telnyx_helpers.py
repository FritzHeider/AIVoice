"""FakeTelnyx test helpers: an in-process client, a recording webhook receiver, builders, signature check."""

from __future__ import annotations

import base64
from collections import deque
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final

import httpx
import orjson
from nacl.signing import VerifyKey
from sim_helpers import LATENCIES
from starlette.applications import Starlette

from va_sim.clock import AcceleratedClock
from va_sim.scenario import load_scenario
from va_sim.server import simulator_routes
from va_sim.settings import WebhookDefaults
from va_sim.state import SimState
from va_sim.telnyx.store import DEFAULT_API_KEY

VOICE_URL: Final = "http://edge:8100/telnyx/voice"
MESSAGING_URL: Final = "http://edge:8100/telnyx/messaging"
APP_URL: Final = "http://edge:8100/telnyx/voice/app"
PROFILE_URL: Final = "http://edge:8100/telnyx/messaging/profile"


@dataclass(frozen=True, slots=True)
class Received:
    """One webhook request the receiver saw, with the sim clock reading when it arrived."""

    url: str
    headers: httpx.Headers
    body: bytes
    virtual_ms: int

    @property
    def envelope(self) -> dict[str, Any]:
        parsed: dict[str, Any] = orjson.loads(self.body)
        return parsed

    @property
    def event_type(self) -> str:
        event_type: str = self.envelope["data"]["event_type"]
        return event_type


class RecordingTransport(httpx.AsyncBaseTransport):
    """A webhook receiver answering from a programmable list (status code, or ``None`` = connection error)."""

    def __init__(self, answers: Iterable[int | None] = ()) -> None:
        self.answers: deque[int | None] = deque(answers)
        self.received: list[Received] = []
        self.clock: AcceleratedClock | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        virtual_ms = self.clock.virtual_ms() if self.clock is not None else 0
        self.received.append(Received(str(request.url), request.headers, body, virtual_ms))
        answer = self.answers.popleft() if self.answers else 200
        if answer is None:
            msg = "receiver unreachable"
            raise httpx.ConnectError(msg, request=request)
        return httpx.Response(answer)

    def of_type(self, event_type: str) -> list[Received]:
        return [item for item in self.received if item.event_type == event_type]


def telnyx_state(transport: RecordingTransport | None = None, *, seed: int = 0) -> SimState:
    clock = AcceleratedClock()
    if transport is not None:
        transport.clock = clock
    return SimState(
        clock=clock,
        latencies=LATENCIES,
        seed=seed,
        webhook_defaults=WebhookDefaults(voice_url=VOICE_URL, messaging_url=MESSAGING_URL),
        webhook_transport=transport,
    )


def advance(state: SimState, seconds: float) -> None:
    """Move the accelerated clock of a :func:`telnyx_state` forward."""
    assert isinstance(state.clock, AcceleratedClock)
    state.clock.advance(seconds)


@asynccontextmanager
async def telnyx_client(state: SimState, api_key: str = DEFAULT_API_KEY) -> AsyncIterator[httpx.AsyncClient]:
    """A client of the FakeTelnyx app authenticated with ``api_key``; closes the state afterwards."""
    app = Starlette(routes=simulator_routes("telnyx", state))
    headers = {"authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://sim", headers=headers
        ) as client:
            yield client
    finally:
        await state.aclose()


def error_of(response: httpx.Response) -> tuple[int, str, str | None]:
    """(status, first error code, its pointer or parameter)."""
    [first, *_] = response.json()["errors"]
    source = first.get("source", {})
    return response.status_code, first["code"], source.get("pointer", source.get("parameter"))


def verify_signature(public_key_b64: str, headers: httpx.Headers, body: bytes) -> None:
    """Independent Ed25519 check with pynacl; raises ``nacl.exceptions.BadSignatureError`` on mismatch."""
    timestamp = headers["telnyx-timestamp"]
    signature = base64.b64decode(headers["telnyx-signature-ed25519"])
    VerifyKey(base64.b64decode(public_key_b64)).verify(f"{timestamp}|".encode() + body, signature)


async def create_app(client: httpx.AsyncClient, **fields: Any) -> dict[str, Any]:
    body = {"application_name": "voiceagents-test", "webhook_event_url": APP_URL, **fields}
    response = await client.post("/v2/call_control_applications", json=body)
    assert response.status_code == 201, response.text
    app: dict[str, Any] = response.json()["data"]
    return app


async def create_messaging_profile(client: httpx.AsyncClient, **fields: Any) -> dict[str, Any]:
    body = {"name": "voiceagents-test", "whitelisted_destinations": ["US"], **fields}
    response = await client.post("/v2/messaging_profiles", json=body)
    assert response.status_code == 200, response.text
    profile: dict[str, Any] = response.json()["data"]
    return profile


async def own_number(
    client: httpx.AsyncClient, state: SimState, phone_number: str, **order: Any
) -> dict[str, Any]:
    """Order ``phone_number``, let the order complete, and return the owned ``phone_number`` record."""
    response = await client.post(
        "/v2/number_orders", json={"phone_numbers": [{"phone_number": phone_number}], **order}
    )
    assert response.status_code == 200, response.text
    await state.tasks.drain()
    listed = await client.get("/v2/phone_numbers", params={"filter[phone_number]": phone_number})
    [record] = listed.json()["data"]
    number: dict[str, Any] = record
    return number


CALLER: Final = "+14155552000"
CALLEE: Final = "+14155550101"


async def at(state: SimState, seconds: float) -> None:
    """Sleep on the sim clock until virtual time ``seconds``; everything due by then (and every in-process
    webhook it triggers) has run when this returns.

    The first zero sleep lets tasks spawned by the last request register their timers, so a timer due exactly
    at ``seconds`` is ordered before this sleeper (an in-process request can finish without yielding to them).
    """
    await state.clock.sleep(0)
    await state.clock.sleep(max(0.0, seconds - state.clock.virtual_ms() / 1000))
    await state.call_engine.idle()


async def settle(state: SimState) -> None:
    """Let in-flight work finish: everything runnable now, then every scheduled call webhook (a recording is
    encoded in a worker thread, which leaves the loop idle, so the clock alone does not wait for it)."""
    await state.clock.sleep(0)
    await state.call_engine.idle()


def register(state: SimState, yaml_text: str) -> str:
    """Register a scenario given as YAML; returns its id."""
    scenario = load_scenario(yaml_text)
    state.register_scenario(scenario)
    return scenario.id


async def dial(client: httpx.AsyncClient, connection_id: str, **overrides: Any) -> dict[str, Any]:
    """``POST /v2/calls`` from :data:`CALLER` to :data:`CALLEE` (overridable); returns ``data``."""
    body = {"connection_id": connection_id, "to": CALLEE, "from": CALLER, **overrides}
    response = await client.post("/v2/calls", json=body)
    assert response.status_code == 200, response.text
    call: dict[str, Any] = response.json()["data"]
    return call


async def command(client: httpx.AsyncClient, ccid: str, action: str, **body: Any) -> httpx.Response:
    return await client.post(f"/v2/calls/{ccid}/actions/{action}", json=body)


async def ok(client: httpx.AsyncClient, ccid: str, action: str, **body: Any) -> None:
    """Run a call command that must succeed."""
    response = await command(client, ccid, action, **body)
    assert response.status_code == 200, response.text
    assert response.json() == {"data": {"result": "ok"}}


async def summary(client: httpx.AsyncClient, ccid: str) -> dict[str, Any]:
    response = await client.get(f"/sim/telnyx/calls/{ccid}")
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


def events(transport: RecordingTransport, ccid: str | None = None) -> list[tuple[str, int]]:
    """(event type, arrival virtual ms) of the call webhooks received, optionally for one leg."""
    return [
        (item.event_type, item.virtual_ms)
        for item in transport.received
        if "call_control_id" in item.envelope["data"]["payload"]
        and (ccid is None or item.envelope["data"]["payload"]["call_control_id"] == ccid)
    ]


def payloads(transport: RecordingTransport, event_type: str) -> list[dict[str, Any]]:
    return [item.envelope["data"]["payload"] for item in transport.of_type(event_type)]


INBOUND_NUMBER: Final = "+14155552001"


async def inbound_call(
    client: httpx.AsyncClient, state: SimState, connection_id: str, **body: Any
) -> dict[str, Any]:
    """Own :data:`INBOUND_NUMBER` on ``connection_id`` and place a call to it from :data:`CALLEE`."""
    if state.telnyx.owner_of(INBOUND_NUMBER) is None:
        await own_number(client, state, INBOUND_NUMBER, connection_id=connection_id)
    response = await client.post("/sim/telnyx/inbound", json={"to": INBOUND_NUMBER, "from": CALLEE, **body})
    assert response.status_code == 202, response.text
    ids: dict[str, Any] = response.json()
    return ids


async def answered_call(client: httpx.AsyncClient, state: SimState, **dial_fields: Any) -> tuple[str, str]:
    """An outgoing call answered at 1 s (no scenario); returns (connection id, call_control_id)."""
    app = await create_app(client)
    call = await dial(client, app["id"], **dial_fields)
    await at(state, 1.0)
    return app["id"], call["call_control_id"]


async def ringing_call(client: httpx.AsyncClient) -> str:
    """An outgoing call that is not answered yet (default outcome: answered at 1 s); its call_control_id."""
    app = await create_app(client)
    ccid: str = (await dial(client, app["id"]))["call_control_id"]
    return ccid


async def ringing_inbound(client: httpx.AsyncClient, state: SimState) -> str:
    """An inbound call waiting for the platform's first command; its call_control_id."""
    app = await create_app(client)
    ccid: str = (await inbound_call(client, state, app["id"]))["call_control_id"]
    return ccid
