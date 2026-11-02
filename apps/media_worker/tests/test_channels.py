"""``ChannelHub``: one pub/sub connection routes every call's control and carrier messages (specs/07 §3
step 6, §17; specs/07 §3 Decision P05-T07b).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import fakeredis
import pytest
from clock_helpers import ManualClock
from redis.exceptions import ConnectionError as RedisConnectionError
from service_helpers import json_log_stream, log_events

from va_domain.carrier import CarrierEvent, carrier_channel
from va_domain.control import SayControl, StopControl, control_channel
from va_media.channels import (
    CARRIER_DEDUPE_SIZE,
    CARRIER_QUEUE_SIZE,
    CONFIRM_TIMEOUT_S,
    CallFeed,
    ChannelError,
    ChannelHub,
    ControlDelivery,
)

ORG_ID = uuid.UUID("0190a5f2-0000-7000-8000-0000000000aa")


@dataclass
class HubRig:
    redis: fakeredis.FakeAsyncRedis
    clock: ManualClock
    hub: ChannelHub


@asynccontextmanager
async def hub_rig(*, run: bool = True) -> AsyncIterator[HubRig]:
    redis = fakeredis.FakeAsyncRedis()
    clock = ManualClock()
    hub = ChannelHub(redis, clock)
    task = asyncio.ensure_future(hub.run()) if run else None
    try:
        yield HubRig(redis, clock, hub)
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await hub.aclose()


def carrier(call_id: uuid.UUID, event_id: str, *, kind: str = "call.answered") -> str:
    return CarrierEvent(
        type=kind,
        leg="primary",
        call_id=call_id,
        org_id=ORG_ID,
        telnyx_event_id=event_id,
        occurred_at=datetime(2026, 11, 2, 12, 0, tzinfo=UTC),
        payload={"call_control_id": "cc-1"},
    ).model_dump_json()


async def next_control(feed: CallFeed) -> ControlDelivery:
    async with asyncio.timeout(5):
        return await feed.next_control()


async def next_carrier(feed: CallFeed) -> CarrierEvent:
    async with asyncio.timeout(5):
        return await feed.next_carrier()


async def published(redis: fakeredis.FakeAsyncRedis, channel: str, data: str) -> None:
    assert await redis.publish(channel, data) == 1


async def test_subscribe_confirms_both_channels_and_routes_control() -> None:
    async with hub_rig() as r:
        call_id = uuid.uuid4()
        feed = await r.hub.subscribe(call_id)
        assert r.hub.subscribed_channels == {control_channel(call_id), carrier_channel(call_id)}
        message = StopControl(id=uuid.uuid4(), reason="api")
        await published(r.redis, control_channel(call_id), message.model_dump_json())
        delivery = await next_control(feed)
        assert delivery == ControlDelivery(message, local=False)


async def test_carrier_events_are_deduped_and_filtered() -> None:
    stream = json_log_stream()
    async with hub_rig() as r:
        call_id = uuid.uuid4()
        feed = await r.hub.subscribe(call_id)
        channel = carrier_channel(call_id)
        await published(r.redis, channel, carrier(call_id, "ev-1"))
        await published(r.redis, channel, carrier(call_id, "ev-1"))
        await published(r.redis, channel, carrier(uuid.uuid4(), "ev-2"))
        await published(r.redis, channel, "{not json")
        await published(r.redis, channel, carrier(call_id, "ev-3", kind="call.hangup"))
        assert (await next_carrier(feed)).telnyx_event_id == "ev-1"
        assert (await next_carrier(feed)).type == "call.hangup"
    details = [e["detail"] for e in log_events(stream) if e["event"] == "media.carrier_invalid"]
    assert details == ["other_call", "malformed"]


async def test_invalid_control_is_dropped_and_logged() -> None:
    stream = json_log_stream()
    async with hub_rig() as r:
        call_id = uuid.uuid4()
        feed = await r.hub.subscribe(call_id)
        await published(r.redis, control_channel(call_id), json.dumps({"type": "dance"}))
        say = SayControl(id=uuid.uuid4(), text="hello")
        await published(r.redis, control_channel(call_id), say.model_dump_json())
        assert (await next_control(feed)).message == say
    event = next(e for e in log_events(stream) if e["event"] == "media.control_invalid")
    assert event["call_id"] == str(call_id)
    assert "dance" not in json.dumps(event)


def test_carrier_overflow_drops_the_oldest() -> None:
    stream = json_log_stream()
    call_id = uuid.uuid4()
    feed = CallFeed(call_id)
    for index in range(CARRIER_QUEUE_SIZE + 2):
        feed.on_carrier(carrier(call_id, f"ev-{index}"))
    queued = [feed._carrier[i].telnyx_event_id for i in (0, -1)]
    assert queued == ["ev-2", f"ev-{CARRIER_QUEUE_SIZE + 1}"]
    assert len([e for e in log_events(stream) if e["event"] == "media.carrier_overflow"]) == 2


async def test_dedupe_remembers_the_last_512_ids() -> None:
    call_id = uuid.uuid4()
    feed = CallFeed(call_id)
    for index in range(CARRIER_DEDUPE_SIZE + 1):
        feed.on_carrier(carrier(call_id, f"ev-{index}"))
        await feed.next_carrier()
    feed.on_carrier(carrier(call_id, f"ev-{CARRIER_DEDUPE_SIZE}"))  # remembered → dropped
    feed.on_carrier(carrier(call_id, "ev-0"))  # forgotten → delivered again
    assert (await next_carrier(feed)).telnyx_event_id == "ev-0"


async def test_calls_are_isolated_and_unsubscribe_stops_delivery() -> None:
    async with hub_rig() as r:
        first, second = uuid.uuid4(), uuid.uuid4()
        feed_one = await r.hub.subscribe(first)
        feed_two = await r.hub.subscribe(second)
        message = StopControl(id=uuid.uuid4())
        await published(r.redis, control_channel(second), message.model_dump_json())
        assert (await next_control(feed_two)).message == message
        await r.hub.unsubscribe(feed_two)
        assert r.hub.subscribed_channels == set(feed_one.channels)
        assert await r.redis.publish(control_channel(second), message.model_dump_json()) == 0
        await r.hub.unsubscribe(feed_one)
        assert r.hub.subscribed_channels == set()
        again = await r.hub.subscribe(first)  # the reader wakes up again after going idle
        await published(r.redis, control_channel(first), message.model_dump_json())
        assert (await next_control(again)).message == message
        assert feed_one._control.empty()


async def test_local_delivery_is_marked_local() -> None:
    feed = CallFeed(uuid.uuid4())
    message = StopControl(id=uuid.uuid4())
    feed.deliver_local(message)
    assert await next_control(feed) == ControlDelivery(message, local=True)


async def test_confirmation_timeout_raises_and_unsubscribes() -> None:
    async with hub_rig(run=False) as r:
        call_id = uuid.uuid4()
        attempt = asyncio.ensure_future(r.hub.subscribe(call_id))
        await r.clock.wait_for_sleep(CONFIRM_TIMEOUT_S)
        await r.clock.run_for(CONFIRM_TIMEOUT_S)
        with pytest.raises(ChannelError) as caught:
            await attempt
        assert caught.value.reason == "timeout"
        assert r.hub.subscribed_channels == set()


async def test_subscribe_redis_error_raises() -> None:
    server = fakeredis.FakeServer()
    server.connected = False
    hub = ChannelHub(fakeredis.FakeAsyncRedis(server=server), ManualClock())
    with pytest.raises(ChannelError) as caught:
        await hub.subscribe(uuid.uuid4())
    assert caught.value.reason == "redis"
    assert hub.subscribed_channels == set()


class FailingPubSub:
    """Wraps a pub/sub whose ``get_message`` raises once; ``unsubscribe`` can be made to fail."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.fail_reads = 1
        self.fail_unsubscribe = False
        self.fail_close = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def get_message(self, **kwargs: Any) -> Any:
        if self.fail_reads:
            self.fail_reads -= 1
            msg = "connection reset"
            raise RedisConnectionError(msg)
        return await self.inner.get_message(**kwargs)

    async def unsubscribe(self, *channels: str) -> None:
        if self.fail_unsubscribe:
            msg = "connection reset"
            raise RedisConnectionError(msg)
        await self.inner.unsubscribe(*channels)

    async def aclose(self) -> None:
        if self.fail_close:
            msg = "connection reset"
            raise RedisConnectionError(msg)
        await self.inner.aclose()


async def test_reader_error_is_logged_and_retried_after_1_s() -> None:
    stream = json_log_stream()
    async with hub_rig() as r:
        failing = FailingPubSub(r.hub._pubsub)
        r.hub._pubsub = failing  # type: ignore[assignment]
        call_id = uuid.uuid4()
        attempt = asyncio.ensure_future(r.hub.subscribe(call_id))
        await r.clock.wait_for_sleep(1.0)
        assert not attempt.done()
        await r.clock.run_for(1.0)
        feed = await attempt
        message = StopControl(id=uuid.uuid4())
        await published(r.redis, control_channel(call_id), message.model_dump_json())
        assert (await next_control(feed)).message == message
        failing.fail_unsubscribe = True
        await r.hub.unsubscribe(feed)
        assert r.hub.subscribed_channels == set()
        failing.fail_close = True
    events = [e["event"] for e in log_events(stream)]
    assert "media.channels_failed" in events
    assert "media.channels_unsubscribe_failed" in events
    assert "media.channels_close_failed" in events


async def test_messages_for_unknown_channels_and_other_types_are_ignored() -> None:
    async with hub_rig(run=False) as r:
        hub = r.hub
        hub._dispatch({"type": "message", "channel": b"call:{x}:control", "data": b"{}"})
        hub._dispatch({"type": "psubscribe", "channel": b"x", "data": 1})
        hub._dispatch({"type": "subscribe", "channel": b"unknown", "data": 1})
        assert hub.subscribed_channels == set()
        call_id = uuid.uuid4()
        attempt = asyncio.ensure_future(hub.subscribe(call_id))
        await r.clock.wait_for_sleep(CONFIRM_TIMEOUT_S)
        for channel in (control_channel(call_id), carrier_channel(call_id)):
            hub._dispatch({"type": "subscribe", "channel": channel, "data": 1})
        feed = await attempt
        hub._dispatch({"type": "message", "channel": control_channel(call_id), "data": 7})
        assert feed._control.empty()
