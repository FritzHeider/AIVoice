"""va_telnyx test helpers: client builders (respx and FakeTelnyx), a sleep-recording clock, fixed jitter."""

from __future__ import annotations

import random
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final

import fakeredis
import httpx
import orjson
from clock_helpers import RecordingClock
from opentelemetry.trace import TracerProvider
from starlette.applications import Starlette

from va_sim.server import simulator_routes
from va_sim.state import SimState
from va_sim.telnyx.store import DEFAULT_API_KEY
from va_telnyx import ClientState, LegRole, NumberSearch, TelnyxClient, TelnyxMetrics, TelnyxRateLimiter
from va_telnyx.client_state import encode

__all__ = [
    "API_KEY",
    "BASE_URL",
    "CALL_ID",
    "CCID",
    "ORG_ID",
    "SIM_BASE_URL",
    "MidJitter",
    "RecordingClock",
    "Rig",
    "body_of",
    "leg_state",
    "provisioned",
    "rig",
    "sim_clients",
    "sim_http",
    "sms_number",
]

API_KEY: Final = "KEY-va-telnyx-tests"
BASE_URL: Final = "https://api.telnyx.test/v2"
CCID: Final = "v3:u5OAKGEPT3Dx8SZSSDRWEMdNH2OripQhO"
CALL_ID: Final = uuid.UUID("0192f3a0-6c1e-7b52-9d4e-3f1a2b3c4d5e")
ORG_ID: Final = uuid.UUID("0192f3a0-0000-7000-8000-000000000001")
SIM_BASE_URL: Final = "http://sim/v2"


class MidJitter(random.Random):
    """Jitter fixed at the middle of its range, so retry delays are exact."""

    def uniform(self, a: float, b: float) -> float:
        return (a + b) / 2


@dataclass(slots=True)
class Rig:
    client: TelnyxClient
    clock: RecordingClock
    limiter_clock: RecordingClock
    redis: fakeredis.FakeAsyncRedis


def rig(
    http: httpx.AsyncClient,
    *,
    base_url: str = BASE_URL,
    api_key: str = API_KEY,
    metrics: TelnyxMetrics | None = None,
    tracer_provider: TracerProvider | None = None,
) -> Rig:
    """A client with fakeredis buckets, recording clocks (client retries and limiter waits apart), mid
    jitter."""
    redis = fakeredis.FakeAsyncRedis()
    clock, limiter_clock = RecordingClock(), RecordingClock()
    client = TelnyxClient(
        api_key,
        base_url,
        http,
        TelnyxRateLimiter(redis, clock=limiter_clock),
        metrics=metrics,
        clock=clock,
        rng=MidJitter(),
        tracer_provider=tracer_provider,
    )
    return Rig(client, clock, limiter_clock, redis)


def body_of(request: httpx.Request) -> dict[str, Any]:
    body: dict[str, Any] = orjson.loads(request.content)
    return body


async def provisioned(client: TelnyxClient, voice_url: str, sms_url: str) -> tuple[str, str]:
    """(call control app id, messaging profile id) created through ``client``."""
    profile = await client.create_outbound_voice_profile("voiceagents-test")
    app = await client.create_call_control_app(
        "voiceagents-test", voice_url, outbound_voice_profile_id=profile.id
    )
    messaging = await client.create_messaging_profile("voiceagents-test", sms_url)
    return app.id, messaging.id


async def sms_number(client: TelnyxClient, state: SimState, voice_url: str, sms_url: str) -> tuple[str, str]:
    """(an owned SMS number on a new messaging profile, that profile's id); the number order has completed."""
    app_id, profile_id = await provisioned(client, voice_url, sms_url)
    [number] = await client.search_numbers(NumberSearch(features=("sms",), limit=1))
    await client.create_number_order(
        [number.phone_number],
        connection_id=app_id,
        customer_reference=str(ORG_ID),
        messaging_profile_id=profile_id,
    )
    await state.tasks.drain()
    return number.phone_number, profile_id


def leg_state(leg: LegRole = "primary") -> str:
    return encode(ClientState(call_id=CALL_ID, leg=leg, org_id=ORG_ID))


@asynccontextmanager
async def sim_http(state: SimState) -> AsyncIterator[tuple[httpx.AsyncClient, httpx.AsyncClient]]:
    """(raw FakeTelnyx client for setup and ``/sim`` inspection, a bare client for va_telnyx clients) on one
    in-process app; closes the sim state on exit."""
    app = Starlette(routes=simulator_routes("telnyx", state))
    transport = httpx.ASGITransport(app=app)
    headers = {"authorization": f"Bearer {DEFAULT_API_KEY}"}
    try:
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://sim", headers=headers) as raw,
            httpx.AsyncClient(transport=transport) as http,
        ):
            yield raw, http
    finally:
        await state.aclose()


@asynccontextmanager
async def sim_clients(state: SimState) -> AsyncIterator[tuple[httpx.AsyncClient, TelnyxClient]]:
    """(raw FakeTelnyx client for setup and ``/sim`` inspection, :class:`TelnyxClient` on the same app)."""
    async with sim_http(state) as (raw, http):
        yield raw, rig(http, base_url=SIM_BASE_URL, api_key=DEFAULT_API_KEY).client
