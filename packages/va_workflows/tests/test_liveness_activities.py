"""Liveness activities: the heartbeat watch and the presence probe (specs/01 §3, §2.1 Decision P05-T06c)."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from clock_helpers import ManualClock
from fakeredis import FakeAsyncRedis
from service_helpers import json_log_stream, log_events
from telephony_rig import broken_redis
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from va_domain.carrier import heartbeat_key, owner_key
from va_workflows.activities.errors import STATE_UNAVAILABLE
from va_workflows.activities.inputs import CallActivityInput
from va_workflows.activities.liveness import PRESENCE_UNAVAILABLE_MESSAGE, CallLivenessActivities
from va_workflows.activities.liveness_inputs import CallPresence, WatchInput, WatchResult, watch_timeout

CALL = CallActivityInput(call_id=uuid.uuid4(), org_id=uuid.uuid4())
WATCH = WatchInput(call_id=CALL.call_id, org_id=CALL.org_id)


class Watch:
    """The watch activity running in a task on a :class:`ManualClock`, with its Temporal heartbeats."""

    def __init__(self, redis: FakeAsyncRedis) -> None:
        self.clock = ManualClock()
        self.env = ActivityEnvironment()
        self.heartbeats = 0
        self.env.on_heartbeat = self._beat
        activities = CallLivenessActivities(redis=redis, clock=self.clock)
        self.task = asyncio.create_task(self.env.run(activities.watch_call_heartbeat, WATCH))

    def _beat(self, *_details: object) -> None:
        self.heartbeats += 1

    async def polls(self, count: int) -> None:
        """Let ``count`` 3 s probe intervals pass."""
        for _ in range(count):
            await self.clock.wait_for_sleep(3.0)
            await self.clock.run_for(3.0)

    async def stop(self) -> None:
        self.env.cancel()
        with pytest.raises(asyncio.CancelledError):
            await self.task


async def test_a_present_heartbeat_keeps_the_watch_running() -> None:
    redis = FakeAsyncRedis()
    await redis.set(heartbeat_key(CALL.call_id), "1")
    watch = Watch(redis)

    await watch.polls(20)

    assert not watch.task.done()
    assert watch.heartbeats == 21
    await watch.stop()


async def test_a_heartbeat_missing_for_20_seconds_ends_the_watch() -> None:
    redis = FakeAsyncRedis()
    await redis.set(heartbeat_key(CALL.call_id), "1")
    watch = Watch(redis)
    await watch.polls(1)
    await redis.delete(heartbeat_key(CALL.call_id))

    await watch.polls(6)
    assert not watch.task.done()
    await watch.polls(1)

    assert await watch.task == WatchResult(reason="heartbeat_lost", missing_s=21.0)


async def test_a_worker_that_never_attaches_is_noticed_after_20_seconds() -> None:
    watch = Watch(FakeAsyncRedis())

    await watch.polls(6)
    assert not watch.task.done()
    await watch.polls(1)

    assert await watch.task == WatchResult(reason="heartbeat_lost", missing_s=21.0)


async def test_a_redis_outage_counts_as_a_present_heartbeat() -> None:
    with json_log_stream() as stream:
        watch = Watch(broken_redis())
        await watch.polls(10)

        assert not watch.task.done()
        await watch.stop()
        failures = [event for event in log_events(stream) if event["event"] == "calls.heartbeat_probe_failed"]

    assert len(failures) == 11
    assert failures[0]["error"] == "ConnectionError"
    assert failures[0]["call_id"] == str(CALL.call_id)


def test_the_watch_outlives_the_call() -> None:
    assert watch_timeout(300).total_seconds() == 900


@pytest.mark.parametrize(("owner", "heartbeat"), [(True, True), (True, False), (False, True), (False, False)])
async def test_presence_reports_the_owner_and_heartbeat_keys(owner: bool, heartbeat: bool) -> None:
    redis = FakeAsyncRedis()
    if owner:
        await redis.set(owner_key(CALL.call_id), "pod-1")
    if heartbeat:
        await redis.set(heartbeat_key(CALL.call_id), "1")
    activities = CallLivenessActivities(redis=redis, clock=ManualClock())

    presence = await ActivityEnvironment().run(activities.call_presence, CALL)

    assert presence == CallPresence(owner=owner, heartbeat=heartbeat)


async def test_presence_fails_retryably_when_redis_is_down() -> None:
    activities = CallLivenessActivities(redis=broken_redis(), clock=ManualClock())

    with pytest.raises(ApplicationError) as caught:
        await ActivityEnvironment().run(activities.call_presence, CALL)

    assert (caught.value.type, caught.value.non_retryable, caught.value.message) == (
        STATE_UNAVAILABLE,
        False,
        PRESENCE_UNAVAILABLE_MESSAGE,
    )
