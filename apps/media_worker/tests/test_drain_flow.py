"""SIGTERM drain: mark the pod, notify sessions, keep serving while calls remain, end the rest at the timeout
(specs/07 §19; specs/07 §3 Decision P05-T07b)."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from clock_helpers import ManualClock
from media_helpers import FakeMediaContainer, close_code, media_app, served, stop_msg
from service_helpers import json_log_stream, log_events, make_client

from va_domain.carrier import owner_key
from va_domain.control import ControlMessage
from va_media.control import DefaultControlHandler
from va_media.drain import DRAINING_PODS_KEY
from va_media.main import DRAIN_END_GRACE_S, MediaState, drain
from va_media.session import CallSession


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


class RecordingHandler(DefaultControlHandler):
    """The default handling, recording each message type."""

    def __init__(self) -> None:
        self.types: list[str] = []
        self.notified = asyncio.Event()

    async def handle(self, session: CallSession, message: ControlMessage) -> bool:
        self.types.append(message.type)
        if message.type == "drain_notice":
            self.notified.set()
        return await super().handle(session, message)


async def test_drain_notifies_sessions_refuses_new_streams_and_waits_for_the_call(models_dir: Path) -> None:
    handler = RecordingHandler()
    async with served(models_dir, control_handler=handler) as s:
        call_id = uuid.uuid4()
        async with s.connect(call_id) as ws:
            session = await s.attach(ws)
            draining = asyncio.ensure_future(drain(s.app))
            async with asyncio.timeout(5):
                await handler.notified.wait()
            assert handler.types == ["drain_notice"]
            async with s.connect(uuid.uuid4()) as refused:
                assert await close_code(refused) == 4503
            assert s.rejected("draining") == 1
            assert not draining.done()
            assert await s.container.redis.keys("*control_ack*") == []  # local notices are never acknowledged
            await ws.send(stop_msg())
            assert await close_code(ws) == 1000
        async with asyncio.timeout(5):
            await draining
        assert session.ownership_lost is False
        assert s.metric("va_media_calls_ended_total", end_reason="stream_closed") == 1


async def test_drain_timeout_ends_remaining_sessions_with_worker_lost(models_dir: Path) -> None:
    stream = json_log_stream()
    async with served(models_dir, media_drain_timeout_s=30) as s:
        call_id = uuid.uuid4()
        async with s.connect(call_id) as ws:
            await s.attach(ws)
            draining = asyncio.ensure_future(drain(s.app))
            await s.clock.wait_for_sleep(30)
            await s.clock.run_for(30)
            assert await close_code(ws) == 1000
            async with asyncio.timeout(5):
                await draining
        assert s.metric("va_media_calls_ended_total", end_reason="worker_lost") == 1
        assert len(s.media.sessions) == 0
        assert await s.container.redis.exists(owner_key(call_id)) == 0
    event = next(e for e in log_events(stream) if e["event"] == "media.drain_timeout")
    assert (event["active_calls"], event["ended_sessions"]) == (1, 1)


async def test_drain_logs_sessions_that_never_finish(models_dir: Path) -> None:
    stream = json_log_stream()
    clock = ManualClock()
    app = media_app(models_dir, clock=clock, media_drain_timeout_s=1)
    media: MediaState = app.state.media
    stuck = StuckSession()
    async with make_client(app):
        media.registry.admit("call-1")
        media.sessions.add(stuck)  # type: ignore[arg-type]
        draining = asyncio.ensure_future(drain(app))
        await clock.wait_for_sleep(1)
        await clock.run_for(1)
        await clock.wait_for_sleep(DRAIN_END_GRACE_S)
        await clock.run_for(DRAIN_END_GRACE_S)
        async with asyncio.timeout(5):
            await draining
    assert stuck.notified == ["drain_notice"]
    assert stuck.ended == [("worker_lost", "error")]
    event = next(e for e in log_events(stream) if e["event"] == "media.drain_unfinished")
    assert event["sessions"] == 1


class StuckSession:
    """A session stand-in that records the drain's calls and never ends."""

    def __init__(self) -> None:
        self.notified: list[str] = []
        self.ended: list[tuple[str, str]] = []

    def notify(self, message: ControlMessage) -> None:
        self.notified.append(message.type)

    def request_end(self, end_reason: str, ended_by: str) -> None:
        self.ended.append((end_reason, ended_by))
