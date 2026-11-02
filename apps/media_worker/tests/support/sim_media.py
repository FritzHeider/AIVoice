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
from media_helpers import FakeContextLoader, MediaKeys, media_app, served_media, stream_query
from sim_helpers import LATENCIES
from starlette.applications import Starlette
from telnyx_helpers import VOICE_URL, RecordingTransport, create_app, dial, telnyx_client

from va_common.time import utcnow
from va_sim.clock import RealtimeClock
from va_sim.settings import WebhookDefaults
from va_sim.state import SimState
from va_sim.telnyx.calls import Call


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


def realtime_telnyx() -> SimState:
    return SimState(
        clock=RealtimeClock(),
        latencies=LATENCIES,
        seed=secrets.randbits(32),
        webhook_defaults=WebhookDefaults(voice_url=VOICE_URL, messaging_url=VOICE_URL),
        webhook_transport=RecordingTransport(),
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
async def streamed_call(
    models_dir: Path,
    body: Any,
    *,
    state: SimState | None = None,
    loader: FakeContextLoader | None = None,
    **dial_overrides: Any,
) -> AsyncIterator[StreamedCall]:
    """Dial a FakeTelnyx call whose media stream attaches to a media worker running ``body`` (``state``: a
    wall-clock sim state shared with other simulators; ``loader``: the call context source)."""
    state = state if state is not None else realtime_telnyx()
    keys = MediaKeys()  # the stream token secret; FakeTelnyx signs the handshake with its own key
    port = free_port()
    app = media_app(
        models_dir,
        keys=keys,
        body=body,
        loader=loader,
        media_public_host=f"127.0.0.1:{port}",
        telnyx_public_key=state.signing_key.public_key_b64,
    )
    call_id = uuid.uuid4()
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
