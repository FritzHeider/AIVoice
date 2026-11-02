"""FakeTelnyx (wall clock) streaming a dialed call to a served media worker."""

from __future__ import annotations

import secrets
import socket
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import orjson
from media_helpers import (
    FakeContextLoader,
    FakeMediaContainer,
    MediaKeys,
    media_app,
    served_media,
    stream_query,
)
from redis.asyncio import Redis
from sim_helpers import LATENCIES
from starlette.applications import Starlette
from telnyx_helpers import VOICE_URL, RecordingTransport, create_app, dial, telnyx_client

from va_common.time import utcnow
from va_domain.carrier import CarrierEvent, carrier_channel, carrier_state_key
from va_sim.clock import RealtimeClock
from va_sim.settings import WebhookDefaults
from va_sim.state import SimState
from va_sim.telnyx.calls import Call


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


# What the edge stores for a premium verdict (va_edge.mapping): answered_by from the raw result.
_PREMIUM_ANSWERED_BY = {
    "human_residence": "human",
    "human_business": "human",
    "machine": "voicemail",
    "fax_detected": "fax",
}
_BEEP_EVENTS = {
    "call.machine.premium.greeting.ended": "beep_detected",
    "call.machine.greeting.ended": "greeting_ended",
}
_DETECTION_EVENTS = {"call.machine.premium.detection.ended", "call.machine.detection.ended"}


class EdgeRelay(RecordingTransport):
    """The webhook receiver standing in for the telephony edge: AMD verdicts and beeps of the routed call go
    into its carrier state hash and onto its carrier channel, as ``va_edge`` stores and publishes them."""

    def __init__(self) -> None:
        super().__init__()
        self.route: tuple[uuid.UUID, Redis] | None = None
        self.relayed: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await super().handle_async_request(request)
        if self.route is not None:
            await self._relay(self.route, orjson.loads(self.received[-1].body)["data"])
        return response

    async def _relay(self, route: tuple[uuid.UUID, Redis], data: dict[str, Any]) -> None:
        call_id, redis = route
        event_type: str = data["event_type"]
        payload: dict[str, Any] = data.get("payload") or {}
        fields: dict[str, str] = {}
        if event_type in _BEEP_EVENTS:
            fields[_BEEP_EVENTS[event_type]] = "1"
        elif event_type == "call.machine.premium.detection.ended":
            result = str(payload.get("result") or "unknown")
            fields = {"amd_result": result, "answered_by": _PREMIUM_ANSWERED_BY.get(result, "unknown")}
        elif event_type not in _DETECTION_EVENTS:
            return
        for name, value in fields.items():
            await redis.hset(carrier_state_key(call_id), name, value)
        event = CarrierEvent(
            type=event_type,
            leg="primary",
            call_id=call_id,
            org_id=uuid.uuid4(),
            telnyx_event_id=str(data.get("id") or secrets.token_hex(8)),
            occurred_at=utcnow(),
            payload=payload,
        )
        await redis.publish(carrier_channel(call_id), event.model_dump_json())
        self.relayed.append(event_type)


def realtime_telnyx(relay: RecordingTransport | None = None) -> SimState:
    return SimState(
        clock=RealtimeClock(),
        latencies=LATENCIES,
        seed=secrets.randbits(32),
        webhook_defaults=WebhookDefaults(voice_url=VOICE_URL, messaging_url=VOICE_URL),
        webhook_transport=relay if relay is not None else RecordingTransport(),
    )


@dataclass
class StreamedCall:
    state: SimState
    app: Starlette
    client: httpx.AsyncClient
    call_id: uuid.UUID
    call: dict[str, Any]

    @property
    def ccid(self) -> str:
        return str(self.call["call_control_id"])

    def sim_call(self) -> Call:
        found = self.state.telnyx.call(self.ccid)
        assert found is not None
        return found[1]


@asynccontextmanager
async def streamed_call(  # noqa: PLR0913 - keyword-only test seams
    models_dir: Path,
    body: Any,
    *,
    state: SimState | None = None,
    loader: FakeContextLoader | None = None,
    answered: bool = False,
    call_id: uuid.UUID | None = None,
    container: FakeMediaContainer | None = None,
    carrier: dict[str, str] | None = None,
    relay: EdgeRelay | None = None,
    **dial_overrides: Any,
) -> AsyncIterator[StreamedCall]:
    """Dial a FakeTelnyx call whose media stream attaches to a media worker running ``body`` (``state``: a
    wall-clock sim state shared with other simulators; ``loader``: the call context source; ``answered``:
    the carrier state already says ``answered``, as the edge would store it; ``call_id``: the call's id, a
    fresh one by default; ``container``: the media container, to share its Redis; ``carrier``: more carrier
    state fields stored before the stream attaches; ``relay``: the state's :class:`EdgeRelay`, routed to this
    call and the container's Redis)."""
    state = state if state is not None else realtime_telnyx()
    keys = MediaKeys()  # the stream token secret; FakeTelnyx signs the handshake with its own key
    port = free_port()
    call_id = call_id if call_id is not None else uuid.uuid4()
    container = container if container is not None else FakeMediaContainer()
    fields = {**({"answered": "1"} if answered else {}), **(carrier or {})}
    for name, value in fields.items():
        await container.redis.hset(carrier_state_key(call_id), name, value)
    if relay is not None:
        relay.route = (call_id, container.redis)
    app = media_app(
        models_dir,
        container,
        keys=keys,
        body=body,
        loader=loader,
        media_public_host=f"127.0.0.1:{port}",
        telnyx_public_key=state.signing_key.public_key_b64,
    )
    token = keys.token(call_id, utcnow())
    async with served_media(app, port=port), telnyx_client(state) as client:
        connection = await create_app(client)
        call = await dial(
            client,
            connection["id"],
            stream_url=f"ws://127.0.0.1:{port}/ws/telnyx?{stream_query(call_id, token)}",
            stream_auth_token=token,
            **dial_overrides,
        )
        yield StreamedCall(state, app, client, call_id, call)
