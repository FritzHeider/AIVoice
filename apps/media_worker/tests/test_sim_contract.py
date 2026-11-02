"""FakeTelnyx opens its media stream to the served media worker: Ed25519 headers, stream token, and
``stream_auth_token`` verify; the session sees the ``start`` facts and ends when the call hangs up."""

from __future__ import annotations

import asyncio
from pathlib import Path

from media_helpers import RecordingBody
from sim_media import streamed_call
from telnyx_helpers import ok

from va_domain.carrier import owner_key
from va_media.session import STREAM_CLOSED


async def test_fake_telnyx_stream_attaches_and_ends_on_hangup(models_dir: Path) -> None:
    body = RecordingBody()
    async with streamed_call(models_dir, body) as streamed:
        session = await body.wait_started()
        assert session.call_id == streamed.call_id
        assert session.start.call_control_id == streamed.ccid
        assert (session.start.media_format.encoding, session.start.media_format.sample_rate) == ("PCMU", 8000)
        assert await streamed.app.state.container.redis.get(owner_key(streamed.call_id)) == b"pod-test"
        await ok(streamed.client, streamed.ccid, "hangup")
        async with asyncio.timeout(5):
            await body.ended.wait()
        assert await streamed.app.state.media.registry.wait_idle(5)
        assert body.ends == [STREAM_CLOSED]
