"""``store_recording`` against Postgres and FakeTelnyx recording URLs (specs/18 §1 step 3; specs/06 §11
Decision P05-T14)."""

from __future__ import annotations

import asyncio
import io
import secrets
import uuid
import wave
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from call_rows import SeededCall, load_call
from fakeredis import FakeAsyncRedis
from recording_rig import (
    BUCKET,
    STARTED_AT,
    MemoryS3,
    activities,
    recording_url,
    saved_input,
    serve_recording,
    wav_bytes,
)
from resolved_configs import resolved_config
from service_helpers import json_log_stream, log_events
from sqlalchemy import update
from telephony_rig import Rig, broken_redis, call_event_rows, failing_factory, funded_org, rig
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from va_common.storage import ServerSideEncryption, recording_copy_key
from va_db.models import Call
from va_db.session import Database
from va_workflows.activities.errors import (
    RECORDING_BUSY,
    STORAGE_UNAVAILABLE,
    TELNYX_UNAVAILABLE,
)
from va_workflows.activities.recording import (
    BUSY_RETRY_MARGIN_MS,
    COPY_LOCK_TTL_MS,
    CopyLockAttempt,
    RecordingCopyLock,
    _Failed,
    _read_wav,
    copy_lock_key,
)
from va_workflows.activities.recording_inputs import StoreRecordingInput, StoreRecordingResult


@pytest.fixture
async def r(database: Database) -> AsyncIterator[Rig]:
    async with rig(database) as built:
        yield built


async def _call(r: Rig, *, settings: dict[str, Any] | None = None, **values: Any) -> SeededCall:
    org = await funded_org(r.database, settings=settings) if settings is not None else None
    defaults: dict[str, Any] = {"answered_at": STARTED_AT - timedelta(milliseconds=1250)}
    return await r.seed("completed", org_id=org, **{**defaults, **values})


async def _run(r: Rig, s3: MemoryS3, inp: StoreRecordingInput, **seams: Any) -> StoreRecordingResult:
    result: StoreRecordingResult = await ActivityEnvironment().run(
        activities(r, s3, **seams).store_recording, inp
    )
    return result


def _entries(directory: Path) -> list[Path]:
    return list(directory.iterdir())


def _events(rows: list[Any], category: str) -> list[dict[str, Any]]:
    return [row.data for row in rows if row.category == category]


async def test_stores_the_wav_records_the_key_and_deletes_the_carrier_copy(r: Rig, tmp_path: Path) -> None:
    call = await _call(r)
    body = wav_bytes(2.5)
    recording_id = serve_recording(r, body)
    s3 = MemoryS3()
    stream = json_log_stream()
    result = await _run(r, s3, saved_input(call, recording_id, unknown_field={"x": 1}), tmp_dir=tmp_path)

    row = await load_call(r.database, call)
    key = recording_copy_key(call.org_id, call.call_id, row.created_at, f"rec:{recording_id}")
    assert key.startswith(f"{call.org_id}/{row.created_at:%Y/%m}/{call.call_id}.")
    assert result == StoreRecordingResult(
        status="stored", s3_key=key, duration_ms=2500, recording_offset_ms=1250
    )
    assert (row.recording_s3_key, row.recording_duration_ms) == (key, 2500)
    stored = s3.objects[(BUCKET, key)]
    assert stored.body == body
    assert stored.extra == {
        "ContentType": "audio/wav",
        "Metadata": {"call_id": str(call.call_id), "org_id": str(call.org_id)},
    }
    events = await call_event_rows(r.database, call)
    assert [(e.message, e.data) for e in events] == [
        ("Recording stored.", {"duration_ms": 2500, "channels": "dual", "recording_offset_ms": 1250})
    ]
    assert recording_id not in r.state.recordings
    assert [req.method for req in r.sent if req.url.path == f"/v2/recordings/{recording_id}"] == ["DELETE"]
    assert _entries(tmp_path) == []
    assert "/sim/recordings/" not in stream.getvalue()
    assert [e["event"] for e in log_events(stream) if e["event"].startswith("recording.")] == [
        "recording.stored",
        "recording.carrier_deleted",
    ]


async def test_sse_headers_are_sent_when_configured(r: Rig) -> None:
    call = await _call(r)
    s3 = MemoryS3()
    sse = ServerSideEncryption("aws:kms", "alias/va")
    await _run(r, s3, saved_input(call, serve_recording(r, wav_bytes(0.5))), sse=sse)
    [stored] = s3.objects.values()
    assert stored.extra["ServerSideEncryption"] == "aws:kms"
    assert stored.extra["SSEKMSKeyId"] == "alias/va"


async def test_the_carrier_copy_stays_when_the_org_keeps_it(r: Rig) -> None:
    call = await _call(r, settings={"delete_carrier_recordings": False})
    recording_id = serve_recording(r, wav_bytes(1))
    result = await _run(r, MemoryS3(), saved_input(call, recording_id))
    assert result.status == "stored"
    assert recording_id in r.state.recordings
    assert not [req for req in r.sent if req.method == "DELETE"]


async def test_a_second_run_is_already_stored_without_upload_or_event(r: Rig) -> None:
    call = await _call(r)
    s3 = MemoryS3()
    inp = saved_input(call, serve_recording(r, wav_bytes(1)))
    first = await _run(r, s3, inp)
    second = await _run(r, s3, inp)
    assert second == StoreRecordingResult(status="already_stored", s3_key=first.s3_key, duration_ms=1000)
    assert s3.uploads == 1
    assert len(_events(await call_event_rows(r.database, call), "call")) == 1


def _other_copy(r: Rig, call: SeededCall) -> Callable[[], Awaitable[None]]:
    """An upload hook: another copy commits the key ``other`` while this run uploads."""

    async def other_copy() -> None:
        async with r.database.tenant_session(call.org_id) as session:
            await session.execute(
                update(Call).where(Call.id == call.call_id).values(recording_s3_key="other")
            )

    return other_copy


async def test_a_key_set_during_the_copy_wins_and_nothing_is_recorded(r: Rig) -> None:
    call = await _call(r)
    recording_id = serve_recording(r, wav_bytes(1))

    s3 = MemoryS3(before_upload=_other_copy(r, call))
    result = await _run(r, s3, saved_input(call, recording_id))
    assert result == StoreRecordingResult(status="already_stored", s3_key="other")
    assert (await load_call(r.database, call)).recording_s3_key == "other"
    assert await call_event_rows(r.database, call) == []
    assert recording_id in r.state.recordings
    assert s3.uploads == 1
    assert len(s3.deleted) == 1
    assert s3.objects == {}


async def test_a_failed_orphan_delete_is_logged_and_still_already_stored(r: Rig) -> None:
    call = await _call(r)

    s3 = MemoryS3(before_upload=_other_copy(r, call), fail_deletes=True)
    stream = json_log_stream()
    result = await _run(r, s3, saved_input(call, serve_recording(r, wav_bytes(1))))
    assert result == StoreRecordingResult(status="already_stored", s3_key="other")
    assert "recording.orphan_delete_failed" in [e["event"] for e in log_events(stream)]


@pytest.mark.parametrize(
    ("payload", "detail"),
    [
        (
            {"recording_urls": {"wav": None, "mp3": "https://x.test/a.mp3"}, "public_recording_urls": {}},
            "no_wav_url",
        ),
        ({"call_control_id": None}, "invalid_payload"),
    ],
)
async def test_unusable_events_fail_with_a_recording_failed_error(
    r: Rig, payload: dict[str, Any], detail: str
) -> None:
    call = await _call(r)
    s3 = MemoryS3()
    result = await _run(r, s3, saved_input(call, serve_recording(r, wav_bytes(1)), **payload))
    assert result == StoreRecordingResult(status="failed", detail=detail)  # type: ignore[arg-type]
    rows = await call_event_rows(r.database, call)
    assert [(row.category, row.level, row.data) for row in rows] == [
        ("error", "warning", {"code": "recording_failed", "detail": detail})
    ]
    assert s3.uploads == 0
    assert (await load_call(r.database, call)).recording_s3_key is None


async def test_the_public_url_is_used_when_the_private_one_is_missing(r: Rig) -> None:
    call = await _call(r)
    recording_id = serve_recording(r, wav_bytes(1))
    inp = saved_input(call, recording_id, recording_urls={"wav": None, "mp3": None})
    assert (await _run(r, MemoryS3(), inp)).status == "stored"


async def test_an_oversized_recording_fails_too_large(r: Rig, tmp_path: Path) -> None:
    call = await _call(r)
    inp = saved_input(call, serve_recording(r, wav_bytes(1)))
    result = await _run(r, MemoryS3(), inp, max_download_bytes=1024, tmp_dir=tmp_path)
    assert result.detail == "too_large"
    assert _events(await call_event_rows(r.database, call), "error") == [
        {"code": "recording_failed", "detail": "too_large"}
    ]
    assert _entries(tmp_path) == []


async def test_a_corrupt_wav_fails_invalid_wav(r: Rig) -> None:
    call = await _call(r)
    result = await _run(r, MemoryS3(), saved_input(call, serve_recording(r, b"not a wav file at all")))
    assert result == StoreRecordingResult(status="failed", detail="invalid_wav")


def test_a_wav_with_a_zero_frame_rate_is_invalid(tmp_path: Path) -> None:
    buffer = io.BytesIO(wav_bytes(0.1))
    raw = bytearray(buffer.getvalue())
    raw[24:28] = (0).to_bytes(4, "little")
    path = tmp_path / "zero.wav"
    path.write_bytes(bytes(raw))
    with pytest.raises(_Failed, match="invalid_wav"):
        _read_wav(path, None)


async def test_the_channel_layout_comes_from_the_file_when_undeclared(r: Rig) -> None:
    call = await _call(r)
    inp = saved_input(call, serve_recording(r, wav_bytes(1, channels=1)), channels=None)
    await _run(r, MemoryS3(), inp)
    [ready] = _events(await call_event_rows(r.database, call), "call")
    assert ready["channels"] == "single"


async def test_unknown_start_or_answer_leaves_the_offset_unknown(r: Rig) -> None:
    call = await _call(r)
    result = await _run(
        r, MemoryS3(), saved_input(call, serve_recording(r, wav_bytes(1)), recording_started_at=None)
    )
    assert (result.status, result.recording_offset_ms) == ("stored", None)
    unanswered = await _call(r, answered_at=None)
    result = await _run(r, MemoryS3(), saved_input(unanswered, serve_recording(r, wav_bytes(1))))
    assert result.recording_offset_ms is None


async def test_a_missing_call_fails_without_an_event(r: Rig) -> None:
    call = await _call(r)
    ghost = SeededCall(org_id=call.org_id, call_id=uuid.uuid4())
    result = await _run(r, MemoryS3(), saved_input(ghost, serve_recording(r, wav_bytes(1))))
    assert result == StoreRecordingResult(status="failed", detail="call_not_found")
    assert await call_event_rows(r.database, ghost) == []


@pytest.mark.parametrize("status", [503, 429])
async def test_transient_download_failures_are_retried(r: Rig, status: int) -> None:
    call = await _call(r)

    async def unavailable(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    with pytest.raises(ApplicationError) as caught:
        await _run(r, MemoryS3(), saved_input(call, "rec-x"), telnyx=failing_factory(r, unavailable))
    assert caught.value.type == TELNYX_UNAVAILABLE
    assert not caught.value.non_retryable
    assert await call_event_rows(r.database, call) == []


async def test_a_dropped_connection_is_retried(r: Rig) -> None:
    call = await _call(r)

    async def reset(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ApplicationError) as caught:
        await _run(r, MemoryS3(), saved_input(call, "rec-x"), telnyx=failing_factory(r, reset))
    assert caught.value.type == TELNYX_UNAVAILABLE


async def test_a_refused_download_fails_download_failed(r: Rig) -> None:
    call = await _call(r)
    result = await _run(r, MemoryS3(), saved_input(call, f"missing-{secrets.token_hex(4)}"))
    assert result == StoreRecordingResult(status="failed", detail="download_failed")


async def test_an_upload_failure_is_retried_and_records_nothing(r: Rig) -> None:
    call = await _call(r)
    recording_id = serve_recording(r, wav_bytes(1))
    with pytest.raises(ApplicationError) as caught:
        await _run(r, MemoryS3(fail_uploads=1), saved_input(call, recording_id))
    assert caught.value.type == STORAGE_UNAVAILABLE
    assert (await load_call(r.database, call)).recording_s3_key is None
    assert await call_event_rows(r.database, call) == []
    assert recording_id in r.state.recordings


async def test_a_failed_carrier_delete_still_stores(r: Rig) -> None:
    config = {**resolved_config("01_task_basic.json"), "telnyx_account_id": str(uuid.uuid4())}
    call = await _call(r, config=config)
    recording_id = serve_recording(r, wav_bytes(1))
    stream = json_log_stream()
    result = await _run(r, MemoryS3(), saved_input(call, recording_id))
    assert result.status == "stored"
    assert recording_id in r.state.recordings
    [failed] = [e for e in log_events(stream) if e["event"] == "recording.carrier_delete_failed"]
    assert failed["error"] == "UnknownTelnyxAccount"


async def test_no_recording_id_means_no_carrier_delete(r: Rig) -> None:
    call = await _call(r)
    recording_id = serve_recording(r, wav_bytes(1))
    inp = saved_input(call, recording_id, payload={"recording_id": None})
    assert (await _run(r, MemoryS3(), inp)).status == "stored"
    assert recording_id in r.state.recordings


def test_wav_builder_matches_the_requested_length() -> None:
    with wave.open(io.BytesIO(wav_bytes(0.25)), "rb") as reader:
        assert (reader.getnframes(), reader.getnchannels()) == (2000, 2)
    assert recording_url("a") == "http://sim/sim/recordings/a.wav"


# Expired presigned links (specs/06 §11 Decision P05-T14 rule 7).


async def test_an_expired_link_is_refreshed_through_the_recordings_api(r: Rig) -> None:
    call = await _call(r)
    body = wav_bytes(1.5)
    recording_id = serve_recording(r, body)
    expired = recording_url(f"expired-{secrets.token_hex(4)}")
    inp = saved_input(
        call, recording_id, recording_urls={"wav": expired}, public_recording_urls={"wav": expired}
    )
    s3 = MemoryS3()
    stream = json_log_stream()
    result = await _run(r, s3, inp)
    assert (result.status, result.duration_ms) == ("stored", 1500)
    [stored] = s3.objects.values()
    assert stored.body == body
    path = f"/v2/recordings/{recording_id}"
    assert [req.method for req in r.sent if req.url.path == path] == ["GET", "DELETE"]
    assert "recording.url_refreshed" in [e["event"] for e in log_events(stream)]
    assert "/sim/recordings/" not in stream.getvalue()


async def test_an_expired_link_without_a_recording_id_fails_without_a_lookup(r: Rig) -> None:
    call = await _call(r)
    serve_recording(r, wav_bytes(1))
    inp = saved_input(call, f"gone-{secrets.token_hex(4)}", payload={"recording_id": None})
    result = await _run(r, MemoryS3(), inp)
    assert result == StoreRecordingResult(status="failed", detail="download_failed")
    assert not [req for req in r.sent if req.url.path.startswith("/v2/recordings/")]


async def test_a_refresh_that_finds_no_recording_fails_download_failed(r: Rig) -> None:
    call = await _call(r)
    missing = f"missing-{secrets.token_hex(4)}"
    result = await _run(r, MemoryS3(), saved_input(call, missing))
    assert result == StoreRecordingResult(status="failed", detail="download_failed")
    assert [req.method for req in r.sent if req.url.path == f"/v2/recordings/{missing}"] == ["GET"]


async def test_a_transient_refresh_failure_is_retried(r: Rig) -> None:
    call = await _call(r)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404 if request.url.path.startswith("/sim/") else 503)

    with pytest.raises(ApplicationError) as caught:
        await _run(r, MemoryS3(), saved_input(call, "rec-x"), telnyx=failing_factory(r, handler))
    assert caught.value.type == TELNYX_UNAVAILABLE
    assert await call_event_rows(r.database, call) == []


# One copy per call (specs/06 §11 Decision P05-T14 rule 4).


async def test_a_different_recording_waits_for_the_running_copy(r: Rig) -> None:
    call = await _call(r)
    redis = FakeAsyncRedis()
    lock = RecordingCopyLock(redis)
    assert (await lock.acquire(call.call_id, "rec:the-first-recording")).taken
    remaining_ms = await redis.pttl(copy_lock_key(call.call_id))
    recording_id = serve_recording(r, wav_bytes(1))
    s3 = MemoryS3()
    with pytest.raises(ApplicationError) as caught:
        await _run(r, s3, saved_input(call, recording_id), copy_lock=lock)
    assert caught.value.type == RECORDING_BUSY
    assert not caught.value.non_retryable
    delay = caught.value.next_retry_delay
    assert delay is not None
    # The retry waits out the holder's lock (a crashed holder frees it only when the TTL expires).
    assert (
        timedelta(milliseconds=remaining_ms)
        <= delay
        <= timedelta(milliseconds=remaining_ms + BUSY_RETRY_MARGIN_MS)
    )
    assert s3.uploads == 0
    assert await call_event_rows(r.database, call) == []
    assert await redis.get(copy_lock_key(call.call_id)) == b"rec:the-first-recording"
    assert recording_id in r.state.recordings


async def test_the_same_recording_takes_its_own_lock_and_releases_it(r: Rig) -> None:
    call = await _call(r)
    redis = FakeAsyncRedis()
    lock = RecordingCopyLock(redis)
    recording_id = serve_recording(r, wav_bytes(1))
    assert (await lock.acquire(call.call_id, f"rec:{recording_id}")).taken  # a crashed earlier attempt
    result = await _run(r, MemoryS3(), saved_input(call, recording_id), copy_lock=lock)
    assert result.status == "stored"
    assert await redis.get(copy_lock_key(call.call_id)) is None


async def test_the_lock_is_released_after_a_failure_and_expires_otherwise(r: Rig) -> None:
    call = await _call(r)
    redis = FakeAsyncRedis()
    lock = RecordingCopyLock(redis)
    result = await _run(r, MemoryS3(), saved_input(call, serve_recording(r, b"not a wav")), copy_lock=lock)
    assert result.detail == "invalid_wav"
    assert await redis.get(copy_lock_key(call.call_id)) is None
    assert (await lock.acquire(call.call_id, "rec:a")).taken
    assert 0 < await redis.pttl(copy_lock_key(call.call_id)) <= COPY_LOCK_TTL_MS
    refused = await lock.acquire(call.call_id, "rec:b")
    assert not refused.taken
    assert 0 < refused.remaining_ms <= COPY_LOCK_TTL_MS
    await lock.release(call.call_id, "rec:b")
    assert await redis.get(copy_lock_key(call.call_id)) == b"rec:a"


class _CommitsFirst(RecordingCopyLock):
    """A lock whose previous holder committed its copy just before this run takes it."""

    def __init__(self, database: Database, call: SeededCall) -> None:
        super().__init__(FakeAsyncRedis())
        self._database = database
        self._call = call

    async def acquire(self, call_id: uuid.UUID, owner: str) -> CopyLockAttempt:
        async with self._database.tenant_session(self._call.org_id) as session:
            await session.execute(
                update(Call)
                .where(Call.id == self._call.call_id)
                .values(recording_s3_key="first", recording_duration_ms=900)
            )
        return await super().acquire(call_id, owner)


async def test_the_key_is_read_again_under_the_lock(r: Rig) -> None:
    call = await _call(r)
    recording_id = serve_recording(r, wav_bytes(1))
    s3 = MemoryS3()
    lock = _CommitsFirst(r.database, call)
    result = await _run(r, s3, saved_input(call, recording_id), copy_lock=lock)
    assert result == StoreRecordingResult(status="already_stored", s3_key="first", duration_ms=900)
    assert s3.uploads == 0
    assert recording_id in r.state.recordings
    assert await call_event_rows(r.database, call) == []


async def test_a_failure_after_another_copy_stored_records_no_error(r: Rig) -> None:
    call = await _call(r)
    async with r.database.tenant_session(call.org_id) as session:
        await session.execute(
            update(Call)
            .where(Call.id == call.call_id)
            .values(recording_s3_key="first", recording_duration_ms=900)
        )
    acts = activities(r, MemoryS3())
    result = await acts._failed(saved_input(call, "rec-gone"), "download_failed")
    assert result == StoreRecordingResult(status="already_stored", s3_key="first", duration_ms=900)
    assert await call_event_rows(r.database, call) == []


async def test_an_unavailable_redis_copies_without_the_lock(r: Rig) -> None:
    call = await _call(r)
    s3 = MemoryS3()
    stream = json_log_stream()
    result = await _run(
        r,
        s3,
        saved_input(call, serve_recording(r, wav_bytes(1))),
        copy_lock=RecordingCopyLock(broken_redis()),
    )
    assert result.status == "stored"
    assert s3.uploads == 1
    assert (await load_call(r.database, call)).recording_s3_key == result.s3_key
    events = [entry["event"] for entry in log_events(stream)]
    assert "recording.lock_unavailable" in events
    assert "recording.unlock_failed" not in events


async def test_a_lock_without_a_ttl_retries_after_the_margin(r: Rig) -> None:
    call = await _call(r)
    redis = FakeAsyncRedis()
    await redis.set(copy_lock_key(call.call_id), "rec:stuck")
    with pytest.raises(ApplicationError) as caught:
        await _run(
            r,
            MemoryS3(),
            saved_input(call, serve_recording(r, wav_bytes(1))),
            copy_lock=RecordingCopyLock(redis),
        )
    assert caught.value.type == RECORDING_BUSY
    assert caught.value.next_retry_delay == timedelta(milliseconds=BUSY_RETRY_MARGIN_MS)


class _MeetingS3(MemoryS3):
    """Both uploads start before either finishes, so both copies race to the guarded UPDATE."""

    def __init__(self) -> None:
        super().__init__()
        self.barrier = asyncio.Barrier(2)

    async def upload_file(
        self,
        filename: str,
        bucket: str,
        key: str,
        ExtraArgs: dict[str, Any] | None = None,  # noqa: N803
    ) -> None:
        async with asyncio.timeout(10):
            await self.barrier.wait()
        await super().upload_file(filename, bucket, key, ExtraArgs)


async def test_two_recordings_copied_without_the_lock_keep_object_and_row_in_step(r: Rig) -> None:
    """Redis down (fail open): two different recordings copy at once; the row names the winner's object,
    which holds the winner's bytes, and the loser's object is deleted with its carrier copy kept."""
    call = await _call(r)
    bodies = {serve_recording(r, wav_bytes(1)): wav_bytes(1), serve_recording(r, wav_bytes(2)): wav_bytes(2)}
    s3 = _MeetingS3()
    lock = RecordingCopyLock(broken_redis())
    results = await asyncio.gather(
        *(_run(r, s3, saved_input(call, recording_id), copy_lock=lock) for recording_id in bodies)
    )
    by_status = {
        result.status: (recording_id, result) for recording_id, result in zip(bodies, results, strict=True)
    }
    assert set(by_status) == {"stored", "already_stored"}
    winner_id, winner = by_status["stored"]
    loser_id, loser = by_status["already_stored"]
    row = await load_call(r.database, call)
    assert row.recording_s3_key is not None
    assert row.recording_s3_key == winner.s3_key == loser.s3_key
    assert s3.uploads == 2
    assert list(s3.objects) == [(BUCKET, row.recording_s3_key)]
    assert s3.objects[(BUCKET, row.recording_s3_key)].body == bodies[winner_id]
    assert s3.deleted == [recording_copy_key(call.org_id, call.call_id, row.created_at, f"rec:{loser_id}")]
    assert winner_id not in r.state.recordings
    assert loser_id in r.state.recordings
    assert len(_events(await call_event_rows(r.database, call), "call")) == 1


async def test_the_same_recording_copied_twice_keeps_the_stored_object(r: Rig) -> None:
    """Two runs of one recording (redeliveries, lock unavailable) upload the same key: the loser must not
    delete it."""
    call = await _call(r)
    recording_id = serve_recording(r, wav_bytes(1))
    s3 = _MeetingS3()
    lock = RecordingCopyLock(broken_redis())
    first, second = await asyncio.gather(
        _run(r, s3, saved_input(call, recording_id), copy_lock=lock),
        _run(r, s3, saved_input(call, recording_id), copy_lock=lock),
    )
    assert sorted([first.status, second.status]) == ["already_stored", "stored"]
    assert s3.uploads == 2
    row = await load_call(r.database, call)
    assert first.s3_key == second.s3_key == row.recording_s3_key
    assert s3.deleted == []
    assert (BUCKET, row.recording_s3_key) in s3.objects
