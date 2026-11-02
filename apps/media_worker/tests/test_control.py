"""Session control listener: dispatch in order, ``control_ack`` writes, end requests, handler failures
(specs/07 §17; specs/07 §3 Decision P05-T07b)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import fakeredis
import pytest
from clock_helpers import ManualClock
from media_helpers import FakeSocket, call_context
from service_helpers import json_log_stream, log_events

from va_common.metrics import Service, build_metrics, new_registry
from va_domain.control import (
    ControlMessage,
    CreditsExhaustedControl,
    DrainNoticeControl,
    InjectContextControl,
    SayControl,
    StopControl,
    TakeoverControl,
    TransferControl,
    control_ack_key,
)
from va_media.channels import CallFeed
from va_media.control import ControlHandler
from va_media.ownership import CallOwnership
from va_media.session import STREAM_CLOSED, CallSession, SessionEnd, StreamStart
from va_telnyx.media import MediaFormat

START = StreamStart("stream-1", "cc-1", MediaFormat(encoding="PCMU", sample_rate=8000, channels=1))


@dataclass
class ControlRig:
    redis: fakeredis.FakeAsyncRedis
    server: fakeredis.FakeServer
    feed: CallFeed
    session: CallSession
    socket: FakeSocket

    def publish(self, message: ControlMessage) -> None:
        self.feed.on_control(message.model_dump_json())

    async def acked(self, message: ControlMessage) -> bool:
        key = control_ack_key(self.session.call_id, message.id)
        return bool(await self.redis.exists(key))


async def forever(session: CallSession) -> SessionEnd:
    await asyncio.Event().wait()
    return STREAM_CLOSED


@asynccontextmanager
async def control_rig(handler: ControlHandler | None = None) -> AsyncIterator[ControlRig]:
    server = fakeredis.FakeServer()
    redis = fakeredis.FakeAsyncRedis(server=server)
    call_id = uuid.uuid4()
    ownership = CallOwnership(redis, call_id, "pod-test")
    assert await ownership.acquire()
    feed = CallFeed(call_id)
    socket = FakeSocket()
    session = CallSession(
        context=call_context(call_id),
        socket=socket,
        start=START,
        ownership=ownership,
        redis=redis,
        clock=ManualClock(),
        metrics=build_metrics(Service.MEDIA_WORKER, new_registry(process_collectors=False)),
        feed=feed,
        control_handler=handler,
    )
    yield ControlRig(redis, server, feed, session, socket)


async def run(session: CallSession) -> SessionEnd:
    async with asyncio.timeout(5):
        return await session.run(forever)


async def test_stop_ends_the_session_and_is_acknowledged() -> None:
    async with control_rig() as r:
        stop = StopControl(id=uuid.uuid4())
        r.publish(stop)
        end = await run(r.session)
        assert end == SessionEnd("ended", end_reason="api_stop", ended_by="api")
        assert await r.acked(stop)
        assert 0 < await r.redis.ttl(control_ack_key(r.session.call_id, stop.id)) <= 30
        assert r.socket.closed_with == 1000


async def test_credits_exhausted_ends_with_insufficient_credits() -> None:
    async with control_rig() as r:
        message = CreditsExhaustedControl(id=uuid.uuid4())
        r.publish(message)
        end = await run(r.session)
        assert (end.end_reason, end.ended_by) == ("insufficient_credits", "system")
        assert await r.acked(message)


async def test_unsupported_messages_are_not_acknowledged_and_order_is_kept() -> None:
    stream = json_log_stream()
    async with control_rig() as r:
        unsupported: list[ControlMessage] = [
            SayControl(id=uuid.uuid4(), text="One moment please."),
            InjectContextControl(id=uuid.uuid4(), text="Order shipped."),
            TransferControl(id=uuid.uuid4(), phone_number="+14155550100"),
            TakeoverControl(id=uuid.uuid4(), phone_number="+14155550100"),
        ]
        notice = DrainNoticeControl(id=uuid.uuid4())
        stop = StopControl(id=uuid.uuid4())
        for message in [*unsupported, notice, stop]:
            r.publish(message)
        await run(r.session)
        assert [await r.acked(m) for m in unsupported] == [False] * 4
        assert await r.acked(notice)
    events = [
        e
        for e in log_events(stream)
        if e["event"].startswith("media.control") or e["event"] == "media.drain_notice"
    ]
    assert [e.get("type", e["event"]) for e in events] == [
        "say",
        "inject_context",
        "transfer",
        "takeover",
        "media.drain_notice",
        "media.control_stop",
    ]
    assert all("One moment" not in str(e) for e in events)


async def test_local_messages_are_handled_but_never_acknowledged() -> None:
    async with control_rig() as r:
        notice = DrainNoticeControl(id=uuid.uuid4())
        stop = StopControl(id=uuid.uuid4())
        r.session.notify(notice)
        r.session.notify(stop)
        end = await run(r.session)
        assert end.end_reason == "api_stop"
        assert not await r.acked(notice)
        assert not await r.acked(stop)


async def test_ack_failure_is_logged_and_the_session_still_ends() -> None:
    stream = json_log_stream()

    class BreakRedisFirst:
        async def handle(self, session: CallSession, message: ControlMessage) -> bool:  # noqa: ARG002
            r.server.connected = False
            session.request_end("api_stop", "api")
            return True

    async with control_rig(BreakRedisFirst()) as r:
        r.publish(StopControl(id=uuid.uuid4()))
        end = await run(r.session)
        assert end.end_reason == "api_stop"
    event = next(e for e in log_events(stream) if e["event"] == "media.control_ack_failed")
    assert event["type"] == "stop"


async def test_handler_exception_fails_the_session_after_cleanup() -> None:
    class Exploding:
        async def handle(self, session: CallSession, message: ControlMessage) -> bool:  # noqa: ARG002
            msg = "handler bug"
            raise RuntimeError(msg)

    async with control_rig(Exploding()) as r:
        r.publish(SayControl(id=uuid.uuid4(), text="hi"))
        with pytest.raises(RuntimeError, match="handler bug"):
            await run(r.session)
        assert r.socket.closed_with == 1011


async def test_invalid_message_is_dropped_without_ack() -> None:
    async with control_rig() as r:
        r.feed.on_control(b'{"id": "nope", "type": "stop"}')
        stop = StopControl(id=uuid.uuid4())
        r.publish(stop)
        end = await run(r.session)
        assert end.end_reason == "api_stop"
        assert await r.redis.keys("*control_ack*") == [control_ack_key(r.session.call_id, stop.id).encode()]
