"""``StreamTailHub``: one reader of the five event bus streams fans new events out to org subscribers
(specs/19-events-and-webhooks.md §9 Decision P06-T06c)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from event_helpers import ORG, event
from fakeredis import FakeAsyncRedis
from redis.exceptions import ConnectionError as RedisConnectionError
from service_helpers import json_log_stream, log_events

from va_events import (
    DomainEvent,
    EventPublisher,
    EventType,
    StreamTailError,
    StreamTailHub,
    SubscriptionClosedError,
    TailSubscription,
)
from va_events.keys import stream_key

OTHER_ORG = uuid.UUID("0193c5d2-0000-7000-8000-000000000002")
CALL = uuid.UUID("0193c5d2-0000-7000-8000-0000000000aa")
WAIT_S = 5.0


class FlakyRedis:
    """fakeredis whose ``xread``/``pipeline`` fail while ``fail_reads``/``fail_start`` is set."""

    def __init__(self, inner: FakeAsyncRedis) -> None:
        self.inner = inner
        self.fail_reads = False
        self.fail_start = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def xread(self, *args: Any, **kwargs: Any) -> Any:
        if self.fail_reads:
            reason = "connection reset"
            raise RedisConnectionError(reason)
        return await self.inner.xread(*args, **kwargs)

    def pipeline(self, *args: Any, **kwargs: Any) -> Any:
        if self.fail_start:
            reason = "connection refused"
            raise RedisConnectionError(reason)
        return self.inner.pipeline(*args, **kwargs)


@pytest.fixture
def redis() -> FlakyRedis:
    return FlakyRedis(FakeAsyncRedis())


@pytest.fixture
async def hub(redis: FlakyRedis) -> AsyncIterator[StreamTailHub]:
    tail = StreamTailHub(redis, queue_size=10)  # type: ignore[arg-type]
    yield tail
    await tail.aclose()


async def publish(redis: FlakyRedis, *events: DomainEvent) -> None:
    publisher = EventPublisher(redis.inner, call_events=None)
    for item in events:
        await publisher.broadcast(item)


async def take(subscription: TailSubscription) -> DomainEvent:
    return await asyncio.wait_for(subscription.next(), WAIT_S)


def running(hub: StreamTailHub) -> bool:
    """``hub.reader_running`` behind a call, so mypy does not narrow the property across asserts."""
    return hub.reader_running


def everything(_: DomainEvent) -> bool:
    return True


def turn(org_id: uuid.UUID = ORG) -> DomainEvent:
    return event(EventType.CALL_TURN, org_id=org_id, call_id=CALL, data={"role": "user", "text": "hi"})


async def test_an_event_reaches_only_its_orgs_subscribers(redis: FlakyRedis, hub: StreamTailHub) -> None:
    mine = await hub.subscribe(ORG, everything)
    theirs = await hub.subscribe(OTHER_ORG, everything)
    other, own = turn(OTHER_ORG), turn()
    await publish(redis, other, own)
    assert (await take(mine)).id == own.id
    assert (await take(theirs)).id == other.id
    assert hub.subscribers() == 2
    assert hub.subscribers(ORG) == 1


async def test_events_added_before_the_first_subscribe_are_not_delivered(
    redis: FlakyRedis, hub: StreamTailHub
) -> None:
    earlier = turn()
    await publish(redis, earlier)
    subscription = await hub.subscribe(ORG, everything)
    later = turn()
    await publish(redis, later)
    assert (await take(subscription)).id == later.id


async def test_events_arrive_in_stream_order_and_every_subscriber_gets_them(
    redis: FlakyRedis, hub: StreamTailHub
) -> None:
    first = await hub.subscribe(ORG, everything)
    second = await hub.subscribe(ORG, everything)
    events = [turn() for _ in range(5)]
    await publish(redis, *events)
    for subscription in (first, second):
        assert [(await take(subscription)).id for _ in events] == [item.id for item in events]


async def test_events_of_every_stream_are_tailed(redis: FlakyRedis, hub: StreamTailHub) -> None:
    subscription = await hub.subscribe(ORG, everything)
    sms = event(EventType.SMS_RECEIVED, data={"from": "+15550001111"})
    billing = event(EventType.BILLING_LOW_BALANCE, data={})
    await publish(redis, sms)
    assert (await take(subscription)).id == sms.id
    await publish(redis, billing)
    assert (await take(subscription)).id == billing.id


async def test_the_predicate_filters_in_the_reader(redis: FlakyRedis, hub: StreamTailHub) -> None:
    subscription = await hub.subscribe(ORG, lambda item: item.type is EventType.CALL_COMPLETED)
    completed = event(EventType.CALL_COMPLETED, call_id=CALL, data={"call_id": str(CALL)})
    await publish(redis, turn(), turn(), completed)
    assert (await take(subscription)).id == completed.id


async def test_a_full_queue_closes_the_subscription_with_overflow(redis: FlakyRedis) -> None:
    hub = StreamTailHub(redis, queue_size=2)  # type: ignore[arg-type]
    try:
        subscription = await hub.subscribe(ORG, everything)
        await publish(redis, turn(), turn(), turn())
        with pytest.raises(SubscriptionClosedError) as raised:
            await take(subscription)
        assert raised.value.reason == "overflow"
    finally:
        await hub.aclose()


async def test_an_undecodable_entry_is_skipped_and_logged(redis: FlakyRedis, hub: StreamTailHub) -> None:
    stream = json_log_stream()
    subscription = await hub.subscribe(ORG, everything)
    await redis.inner.xadd(stream_key("calls"), {"payload": b"{not json"})
    await redis.inner.xadd(stream_key("calls"), {"other": b"x"})
    valid = turn()
    await publish(redis, valid)
    assert (await take(subscription)).id == valid.id
    poison = [entry for entry in log_events(stream) if entry["event"] == "events.stream_tail_poison"]
    assert [entry["stream"] for entry in poison] == ["events:calls", "events:calls"]


async def test_a_redis_error_closes_every_subscription_with_lost_and_a_later_subscribe_restarts(
    redis: FlakyRedis, hub: StreamTailHub
) -> None:
    first = await hub.subscribe(ORG, everything)
    second = await hub.subscribe(OTHER_ORG, everything)
    redis.fail_reads = True
    for subscription in (first, second):
        with pytest.raises(SubscriptionClosedError) as raised:
            await take(subscription)
        assert raised.value.reason == "lost"
    assert not running(hub)
    assert hub.subscribers() == 0
    redis.fail_reads = False
    again = await hub.subscribe(ORG, everything)
    assert running(hub)
    fresh = turn()
    await publish(redis, fresh)
    assert (await take(again)).id == fresh.id


async def test_a_redis_error_at_start_fails_the_subscribe(redis: FlakyRedis, hub: StreamTailHub) -> None:
    redis.fail_start = True
    with pytest.raises(StreamTailError) as raised:
        await hub.subscribe(ORG, everything)
    assert raised.value.reason == "redis"
    assert not running(hub)
    assert hub.subscribers() == 0


async def test_the_last_unsubscribe_stops_the_reader(redis: FlakyRedis, hub: StreamTailHub) -> None:
    first = await hub.subscribe(ORG, everything)
    second = await hub.subscribe(ORG, everything)
    await hub.unsubscribe(first)
    assert running(hub)
    assert first.closed_reason == "closed"
    await hub.unsubscribe(second)
    await hub.unsubscribe(second)
    assert not running(hub)
    assert hub.subscribers() == 0
    resumed = await hub.subscribe(ORG, everything)
    fresh = turn()
    await publish(redis, fresh)
    assert (await take(resumed)).id == fresh.id


async def test_aclose_closes_every_subscription_and_refuses_new_ones(hub: StreamTailHub) -> None:
    subscription = await hub.subscribe(ORG, everything)
    await hub.aclose()
    with pytest.raises(SubscriptionClosedError) as raised:
        await take(subscription)
    assert raised.value.reason == "closed"
    assert not running(hub)
    with pytest.raises(StreamTailError) as refused:
        await hub.subscribe(ORG, everything)
    assert refused.value.reason == "closed"
