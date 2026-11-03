"""``LiveHub``: one pub/sub connection fans each live channel out to every in-process subscriber
(specs/19-events-and-webhooks.md §9 Decision P06-T06a)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import fakeredis
import pytest
from clock_helpers import ManualClock
from redis.exceptions import ConnectionError as RedisConnectionError
from service_helpers import json_log_stream, log_events

from va_events.live_hub import (
    CONFIRM_TIMEOUT_S,
    LiveHub,
    LiveHubError,
    LiveSubscription,
    LiveSubscriptionClosedError,
)

CHANNEL = "call:{0193c5d2-0000-7000-8000-0000000000aa}:live"
OTHER = "call:{0193c5d2-0000-7000-8000-0000000000bb}:live"


def _reset_if(armed: bool) -> None:
    if armed:
        reason = "connection reset"
        raise RedisConnectionError(reason)


class WrappedPubSub:
    """A real fakeredis pub/sub whose reads can fail (``fail_reads`` times) or never return (``hang``), and
    whose ``unsubscribe``/``aclose`` fail while their flag is set."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.fail_reads = 0
        self.hang = False
        self.fail_unsubscribe = False
        self.fail_close = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def get_message(self, **kwargs: Any) -> Any:
        while self.hang:
            await asyncio.Event().wait()
        failing, self.fail_reads = self.fail_reads > 0, max(self.fail_reads - 1, 0)
        _reset_if(failing)
        return await self.inner.get_message(**kwargs)

    async def unsubscribe(self, *channels: str) -> None:
        _reset_if(self.fail_unsubscribe)
        await self.inner.unsubscribe(*channels)

    async def aclose(self) -> None:
        _reset_if(self.fail_close)
        await self.inner.aclose()


class WrappedRedis:
    """fakeredis whose ``pubsub()`` connections are :class:`WrappedPubSub` (recorded in ``opened``)."""

    def __init__(self, redis: fakeredis.FakeAsyncRedis) -> None:
        self.redis = redis
        self.opened: list[WrappedPubSub] = []
        self.next_hang = False

    def pubsub(self) -> WrappedPubSub:
        wrapped = WrappedPubSub(self.redis.pubsub())
        wrapped.hang, self.next_hang = self.next_hang, False
        self.opened.append(wrapped)
        return wrapped


@asynccontextmanager
async def hub_rig(*, queue_size: int = 1000) -> AsyncIterator[tuple[LiveHub, fakeredis.FakeAsyncRedis, Any]]:
    redis = fakeredis.FakeAsyncRedis()
    wrapped = WrappedRedis(redis)
    hub = LiveHub(wrapped, ManualClock(), queue_size=queue_size)  # type: ignore[arg-type]
    try:
        yield hub, redis, wrapped
    finally:
        await hub.aclose()
        await redis.aclose()


async def receive(subscription: LiveSubscription) -> bytes:
    async with asyncio.timeout(5):
        return await subscription.next()


async def closed_reason(subscription: LiveSubscription) -> str:
    async with asyncio.timeout(5):
        with pytest.raises(LiveSubscriptionClosedError) as caught:
            await subscription.next()
    return caught.value.reason


async def numsub(redis: fakeredis.FakeAsyncRedis, channel: str) -> int:
    counts: list[tuple[bytes | str, int]] = await redis.pubsub_numsub(channel)
    return counts[0][1]


async def test_every_subscriber_of_a_channel_gets_each_message_over_one_subscription() -> None:
    async with hub_rig() as (hub, redis, wrapped):
        first = await hub.subscribe(CHANNEL)
        second = await hub.subscribe(CHANNEL)
        other = await hub.subscribe(OTHER)
        assert len(wrapped.opened) == 1
        assert await numsub(redis, CHANNEL) == 1
        assert hub.subscribers(CHANNEL) == 2

        assert await redis.publish(CHANNEL, b"one") == 1
        assert await redis.publish(OTHER, "two") == 1

        assert await receive(first) == b"one"
        assert await receive(second) == b"one"
        assert await receive(other) == b"two"


async def test_the_last_unsubscribe_releases_the_channel() -> None:
    async with hub_rig() as (hub, redis, _):
        first = await hub.subscribe(CHANNEL)
        second = await hub.subscribe(CHANNEL)
        await hub.unsubscribe(first)
        assert await closed_reason(first) == "closed"
        assert hub.subscribed_channels == {CHANNEL}
        assert await numsub(redis, CHANNEL) == 1
        await hub.unsubscribe(second)
        await hub.unsubscribe(second)
        assert hub.subscribed_channels == frozenset()
        assert await numsub(redis, CHANNEL) == 0

        again = await hub.subscribe(CHANNEL)
        await redis.publish(CHANNEL, b"back")
        assert await receive(again) == b"back"


async def test_a_confirmation_timeout_raises_and_forgets_the_channel() -> None:
    async with hub_rig() as (hub, _, wrapped):
        wrapped.next_hang = True
        hub_clock: ManualClock = hub._clock  # type: ignore[assignment]
        attempt = asyncio.ensure_future(hub.subscribe(CHANNEL))
        await hub_clock.wait_for_sleep(CONFIRM_TIMEOUT_S)
        await hub_clock.run_for(CONFIRM_TIMEOUT_S)
        with pytest.raises(LiveHubError) as caught:
            await attempt
        assert caught.value.reason == "timeout"
        assert hub.subscribed_channels == frozenset()


async def test_a_redis_error_on_subscribe_raises() -> None:
    server = fakeredis.FakeServer()
    server.connected = False
    hub = LiveHub(fakeredis.FakeAsyncRedis(server=server), ManualClock())
    with pytest.raises(LiveHubError) as caught:
        await hub.subscribe(CHANNEL)
    assert caught.value.reason == "redis"
    assert hub.subscribed_channels == frozenset()
    await hub.aclose()


async def test_a_full_queue_closes_only_the_slow_subscriber() -> None:
    async with hub_rig(queue_size=2) as (hub, redis, _):
        slow = await hub.subscribe(CHANNEL)
        fast = await hub.subscribe(CHANNEL)
        for index in range(3):
            await redis.publish(CHANNEL, f"m{index}")
            assert await receive(fast) == f"m{index}".encode()
        assert await closed_reason(slow) == "overflow"
        await redis.publish(CHANNEL, b"later")
        assert await receive(fast) == b"later"


async def test_a_lost_connection_ends_every_subscription_and_the_next_subscribe_reconnects() -> None:
    stream = json_log_stream()
    async with hub_rig() as (hub, redis, wrapped):
        first = await hub.subscribe(CHANNEL)
        other = await hub.subscribe(OTHER)
        await redis.publish(CHANNEL, b"before")
        assert await receive(first) == b"before"
        wrapped.opened[0].fail_reads = 1
        wrapped.opened[0].fail_close = True
        await redis.publish(OTHER, b"wakes the reader")

        # The reader was already waiting inside the read that returns this message; the next read fails.
        assert await receive(other) == b"wakes the reader"
        assert await closed_reason(first) == "lost"
        assert await closed_reason(other) == "lost"
        assert hub.subscribed_channels == frozenset()

        fresh = await hub.subscribe(CHANNEL)
        assert len(wrapped.opened) == 2
        await redis.publish(CHANNEL, b"after")
        assert await receive(fresh) == b"after"
    events = [entry["event"] for entry in log_events(stream)]
    assert "events.live_hub_lost" in events
    assert "events.live_hub_close_failed" in events


async def test_unsubscribe_failures_are_logged() -> None:
    stream = json_log_stream()
    async with hub_rig() as (hub, _, wrapped):
        subscription = await hub.subscribe(CHANNEL)
        wrapped.opened[0].fail_unsubscribe = True
        await hub.unsubscribe(subscription)
        assert hub.subscribed_channels == frozenset()
    assert "events.live_unsubscribe_failed" in [entry["event"] for entry in log_events(stream)]


async def test_aclose_ends_subscriptions_and_refuses_new_ones() -> None:
    async with hub_rig() as (hub, _, _wrapped):
        subscription = await hub.subscribe(CHANNEL)
        await hub.aclose()
        assert await closed_reason(subscription) == "closed"
        with pytest.raises(LiveHubError):
            await hub.subscribe(CHANNEL)


async def test_queued_messages_are_read_before_the_close() -> None:
    subscription = LiveSubscription(CHANNEL, 10)
    subscription.deliver(b"a")
    subscription.close("lost")
    subscription.deliver(b"ignored")
    assert await receive(subscription) == b"a"
    assert await closed_reason(subscription) == "lost"
