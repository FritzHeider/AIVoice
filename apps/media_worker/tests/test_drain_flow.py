"""SIGTERM drain: mark the pod, keep serving while calls remain, give up after the timeout."""

from __future__ import annotations

import asyncio
from pathlib import Path

from media_helpers import FakeMediaContainer, media_app
from service_helpers import json_log_stream, log_events, make_client

from va_media.drain import DRAINING_PODS_KEY
from va_media.main import MediaState, drain


async def test_drain_waits_for_last_call(models_dir: Path) -> None:
    container = FakeMediaContainer()
    app = media_app(models_dir, container)
    media: MediaState = app.state.media
    async with make_client(app) as client:
        media.registry.admit("call-1")
        await media.drain.begin(container.redis, "pod-test")  # what drain() does first (idempotent)
        draining = asyncio.ensure_future(drain(app))
        await asyncio.sleep(0)
        assert (await client.get("/readyz")).json()["failing"] == ["draining"]
        assert not draining.done()
        media.registry.release("call-1")
        await asyncio.wait_for(draining, timeout=5)


async def test_drain_timeout_logged_with_remaining_calls(models_dir: Path) -> None:
    stream = json_log_stream()
    container = FakeMediaContainer()
    app = media_app(models_dir, container, media_drain_timeout_s=0.01)
    async with make_client(app):
        app.state.media.registry.admit("call-1")
        await drain(app)
        assert app.state.media.drain.draining
        assert await container.redis.smembers(DRAINING_PODS_KEY) == {b"pod-test"}
    event = next(e for e in log_events(stream) if e["event"] == "media.drain_timeout")
    assert event["active_calls"] == 1
