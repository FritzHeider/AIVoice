"""FakeTelnyx opens its media stream to the served media worker: Ed25519 headers, stream token, and
``stream_auth_token`` verify; the session sees the ``start`` facts and ends when the call hangs up."""

from __future__ import annotations

import asyncio
import secrets
import socket
import uuid
from pathlib import Path

from media_helpers import MediaKeys, RecordingBody, media_app, served_media, stream_query
from sim_helpers import LATENCIES
from telnyx_helpers import VOICE_URL, RecordingTransport, create_app, dial, ok, telnyx_client

from va_common.time import utcnow
from va_domain.carrier import owner_key
from va_media.session import STREAM_CLOSED
from va_sim.clock import RealtimeClock
from va_sim.settings import WebhookDefaults
from va_sim.state import SimState


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


async def test_fake_telnyx_stream_attaches_and_ends_on_hangup(models_dir: Path) -> None:
    state = realtime_telnyx()
    keys = MediaKeys()  # the stream token secret; FakeTelnyx signs the handshake with its own key
    port = free_port()
    body = RecordingBody()
    app = media_app(
        models_dir,
        keys=keys,
        body=body,
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
        )
        session = await body.wait_started()
        assert session.call_id == call_id
        assert session.start.call_control_id == call["call_control_id"]
        assert (session.start.media_format.encoding, session.start.media_format.sample_rate) == ("PCMU", 8000)
        assert await app.state.container.redis.get(owner_key(call_id)) == b"pod-test"
        await ok(client, call["call_control_id"], "hangup")
        async with asyncio.timeout(5):
            await body.ended.wait()
        assert await app.state.media.registry.wait_idle(5)
        assert body.ends == [STREAM_CLOSED]
