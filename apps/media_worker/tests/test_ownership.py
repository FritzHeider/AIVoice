"""The ``call:{<id>}:owner`` lock (specs/07 §3 step 2, §19)."""

from __future__ import annotations

import uuid

import fakeredis

from va_domain.carrier import owner_key
from va_media.ownership import OWNER_TTL_MS, CallOwnership


async def test_acquire_once_per_call() -> None:
    redis = fakeredis.FakeAsyncRedis()
    call_id = uuid.uuid4()
    mine, theirs = CallOwnership(redis, call_id, "pod-a"), CallOwnership(redis, call_id, "pod-b")
    assert await mine.acquire()
    assert not await theirs.acquire()
    assert not await mine.acquire(), "a second acquire by the holder is refused too"
    assert await redis.get(owner_key(call_id)) == b"pod-a"
    assert 0 < await redis.pttl(owner_key(call_id)) <= OWNER_TTL_MS
    assert await CallOwnership(redis, uuid.uuid4(), "pod-b").acquire(), "other calls are independent"


async def test_renew_extends_only_the_owner() -> None:
    redis = fakeredis.FakeAsyncRedis()
    call_id = uuid.uuid4()
    mine, theirs = CallOwnership(redis, call_id, "pod-a"), CallOwnership(redis, call_id, "pod-b")
    await mine.acquire()
    await redis.pexpire(owner_key(call_id), 1000)
    assert not await theirs.renew()
    assert await redis.pttl(owner_key(call_id)) <= 1000
    assert await mine.renew()
    assert await redis.pttl(owner_key(call_id)) > 1000
    await redis.delete(owner_key(call_id))
    assert not await mine.renew(), "a lost lock is not recreated"
    assert await redis.get(owner_key(call_id)) is None


async def test_release_deletes_only_own_value() -> None:
    redis = fakeredis.FakeAsyncRedis()
    call_id = uuid.uuid4()
    mine, theirs = CallOwnership(redis, call_id, "pod-a"), CallOwnership(redis, call_id, "pod-b")
    await mine.acquire()
    assert not await theirs.release()
    assert await redis.get(owner_key(call_id)) == b"pod-a"
    assert await mine.release()
    assert await redis.get(owner_key(call_id)) is None
    assert not await mine.release()
