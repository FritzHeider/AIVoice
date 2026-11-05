"""A busy ``store_recording`` waits out a long copy-lock holder (specs/06 §11 Decision P08-FIX-1): the busy
delay outlasts the policy's 30 s backoff cap in Temporal, and the retry finds the holder's copy already
stored."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from busy_retry_stub import BUSY_ONCE_ACTIVITY, BusyRetryStub
from call_rows import SeededCall
from fakeredis import FakeAsyncRedis
from recording_rig import STARTED_AT, MemoryS3, activities, saved_input, serve_recording, wav_bytes
from sqlalchemy import update
from telephony_rig import Rig, call_event_rows, rig
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Worker
from workflow_rig import workflow_env

from va_db.models import Call
from va_db.session import Database
from va_workflows.activities.errors import RECORDING_BUSY, retryable
from va_workflows.activities.recording import (
    BUSY_MESSAGE,
    BUSY_RETRY_MARGIN_MS,
    COPY_LOCK_TTL_MS,
    RecordingCopyLock,
    copy_lock_key,
)
from va_workflows.activities.recording_inputs import (
    STORE_RECORDING_RETRY,
    STORE_RECORDING_TIMEOUT,
    StoreRecordingInput,
    StoreRecordingResult,
)

_HOLDER_HOLD_MS = 300_000
"""How long the holder still keeps the lock: far beyond the policy's own backoff (2 + 4 + 8 + 16 s)."""
_HOLDER_KEY = "org/2026/11/holder.wav"


@pytest.fixture
async def r(database: Database) -> AsyncIterator[Rig]:
    async with rig(database) as built:
        yield built


async def _run(
    r: Rig, s3: MemoryS3, inp: StoreRecordingInput, lock: RecordingCopyLock
) -> StoreRecordingResult:
    result: StoreRecordingResult = await ActivityEnvironment().run(
        activities(r, s3, copy_lock=lock).store_recording, inp
    )
    return result


async def _holder_commits(r: Rig, call: SeededCall) -> None:
    async with r.database.tenant_session(call.org_id) as session:
        await session.execute(
            update(Call)
            .where(Call.id == call.call_id)
            .values(recording_s3_key=_HOLDER_KEY, recording_duration_ms=900)
        )


async def test_a_long_running_holder_is_waited_out_and_its_copy_is_already_stored(r: Rig) -> None:
    call = await r.seed("completed", answered_at=STARTED_AT - timedelta(milliseconds=1250))
    redis = FakeAsyncRedis()
    lock = RecordingCopyLock(redis)
    holder = "rec:the-long-running-holder"
    assert (await lock.acquire(call.call_id, holder)).taken
    await redis.pexpire(copy_lock_key(call.call_id), _HOLDER_HOLD_MS)
    remaining_ms = await redis.pttl(copy_lock_key(call.call_id))
    inp = saved_input(call, serve_recording(r, wav_bytes(1)))
    s3 = MemoryS3()

    with pytest.raises(ApplicationError) as caught:
        await _run(r, s3, inp, lock)
    assert caught.value.type == RECORDING_BUSY
    assert not caught.value.non_retryable
    delay = caught.value.next_retry_delay
    assert delay is not None
    assert delay >= timedelta(milliseconds=remaining_ms)
    assert delay > timedelta(seconds=30)  # beyond the policy's maximum_interval

    # The holder finishes its copy and releases the lock before the retry runs.
    await _holder_commits(r, call)
    await lock.release(call.call_id, holder)

    result = await _run(r, s3, inp, lock)
    assert result == StoreRecordingResult(status="already_stored", s3_key=_HOLDER_KEY, duration_ms=900)
    assert s3.uploads == 0
    assert await call_event_rows(r.database, call) == []
    assert await redis.get(copy_lock_key(call.call_id)) is None


def test_a_live_holder_never_outlives_its_lock() -> None:
    """The lock outlasts one whole copy attempt, so a busy delay of PTTL + margin waits out a live holder (the
    retries stay inside the post-call budget through the store window, specs/18 §1 Decision P08-FIX-6)."""
    assert timedelta(milliseconds=COPY_LOCK_TTL_MS) > STORE_RECORDING_TIMEOUT
    assert BUSY_RETRY_MARGIN_MS > 0


class _BusyOnce:
    """Refused once (with ``delay`` as the busy delay), then succeeds."""

    def __init__(self, delay: timedelta | None) -> None:
        self.delay = delay
        self.attempts = 0

    @activity.defn(name=BUSY_ONCE_ACTIVITY)
    async def busy_once(self) -> int:
        self.attempts += 1
        attempt = activity.info().attempt
        if attempt == 1:
            raise retryable(BUSY_MESSAGE, RECORDING_BUSY, next_retry_delay=self.delay)
        return attempt


async def _busy_once(delay: timedelta | None) -> tuple[int, float]:
    fake = _BusyOnce(delay)
    queue = f"busy-retry-{uuid.uuid4()}"
    async with (
        workflow_env() as env,
        Worker(env.client, task_queue=queue, workflows=[BusyRetryStub], activities=[fake.busy_once]),
    ):
        attempt, elapsed_s = await env.client.execute_workflow(
            BusyRetryStub.run, id=f"busy-retry-{uuid.uuid4()}", task_queue=queue
        )
    assert fake.attempts == 2
    return attempt, elapsed_s


async def test_temporal_honours_a_busy_delay_beyond_the_policy_cap() -> None:
    """``next_retry_delay`` replaces the policy interval and is not capped by ``maximum_interval`` (30 s)."""
    assert STORE_RECORDING_RETRY.maximum_interval == timedelta(seconds=30)
    attempt, elapsed_s = await _busy_once(timedelta(minutes=5))
    assert attempt == 2
    assert elapsed_s >= timedelta(minutes=5).total_seconds()
    # Control: without a busy delay the policy's first interval (2 s) applies.
    attempt, elapsed_s = await _busy_once(None)
    assert attempt == 2
    assert STORE_RECORDING_RETRY.initial_interval.total_seconds() <= elapsed_s < 30
