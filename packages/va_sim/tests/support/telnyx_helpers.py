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
