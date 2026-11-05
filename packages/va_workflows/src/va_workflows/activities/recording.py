"""``store_recording``: copy a Telnyx recording to object storage (specs/18-post-call-analysis.md §1 step 3;
specs/06-telephony-telnyx.md §11 Decision P05-T14).

The activity takes the ``call.recording.saved`` carrier event, downloads ``recording_urls.wav`` (else
``public_recording_urls.wav``; an mp3 is never copied because the platform always records WAV) to a scratch
file, reads the duration with the stdlib ``wave`` module, uploads the file to
``{S3_BUCKET_RECORDINGS}/{org_id}/{yyyy}/{mm}/{call_id}.{h}.wav`` (metadata ``call_id``/``org_id``, SSE-KMS
with the AWS KMS provider), and in one transaction sets ``calls.recording_s3_key``/``recording_duration_ms``
(guarded by ``recording_s3_key IS NULL``) and records ``call.recording_ready``. After the commit it deletes
the carrier copy when the org's ``delete_carrier_recordings`` (default true) says so; a failed delete is
logged only.

Every recording has its own object: ``recording_copy_key`` (``{org_id}/{yyyy}/{mm}/{call_id}.{h}.wav``,
``h`` from the recording's identity: ``recording_id``, else the Telnyx event id), so the guarded key
``UPDATE`` (``recording_s3_key IS NULL``) always names the bytes it describes. A run whose ``UPDATE`` loses
to a different key deletes its own just-uploaded object and keeps its carrier copy; a run that loses to its
own key (a redelivery of the same recording) keeps the object, which is the stored one.

:class:`RecordingCopyLock` (``call:{id}:reccopy``, owned by the recording's identity) only saves duplicate
work: a redelivery of the same recording may run alongside, a different recording waits (retryable
``RecordingBusy`` with ``next_retry_delay`` = the lock's remaining TTL + 1 s, so the retries outlast a live
or crashed holder) and, once the first copy committed, finds the key set and returns ``already_stored``. The
key is checked again after the lock is taken, and again before a failure is recorded, so a run that lost to
another copy never reports ``recording_failed`` for a stored call. When Redis is unavailable the copy
proceeds without the lock (fail open): per-recording keys keep object and row in step.

The webhook's presigned links expire after a few minutes: a ``403``/``404`` download with a ``recording_id``
fetches fresh ``download_urls`` (``GET /v2/recordings/{id}`` with the call's account) and downloads once more.

The workflow passes ``commit_by`` (its ``schedule_to_close`` deadline; specs/18 §1 Decision P08-FIX-7): an
attempt that starts within ``COMMIT_MARGIN`` of it, or whose upload ends within that margin, raises the
non-retryable ``RecordingOutOfTime`` instead of committing. A late upload's object is deleted and the carrier
copy is kept, no event is recorded, and the call's ``recording = skipped`` in ``call.completed`` stays true
(the activity never heartbeats, so a server-side timeout alone would not stop a running attempt).

Unusable recordings (no WAV URL, refused download, over 2 GiB, not a WAV) return ``failed`` with a
``call.error`` ``recording_failed`` event and are never retried; transient Telnyx and object-store failures
raise retryable errors. Logs never contain the presigned URL.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import uuid
import wave
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import JsonValue, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select, update
from temporalio import activity

from va_common.logging import bound_context, get_logger
from va_common.storage import ObjectStore, ServerSideEncryption, recording_copy_key
from va_common.time import Clock
from va_db.models import Call
from va_db.org_settings import OrgSettingsReader
from va_db.session import Database
from va_events import DomainEvent, EventPublisher, EventType
from va_telnyx import (
    TelnyxApiError,
    TelnyxClient,
    TelnyxClientFactory,
    TelnyxDownloadTooLarge,
    TelnyxError,
    TelnyxRateLimited,
    TelnyxTransportError,
    UnknownTelnyxAccount,
)
from va_telnyx.messaging import MAX_DOWNLOAD_BYTES
from va_telnyx.voice_events import RecordingSaved
from va_workflows.activities.errors import (
    RECORDING_BUSY,
    STORAGE_UNAVAILABLE,
    TELNYX_UNAVAILABLE,
    recording_out_of_time,
    retryable,
)
from va_workflows.activities.events import broadcast_events, call_event, persist_events
from va_workflows.activities.recording_inputs import (
    COMMIT_MARGIN,
    STORE_RECORDING_TIMEOUT,
    RecordingFailure,
    StoreRecordingInput,
    StoreRecordingResult,
)

__all__ = [
    "BUSY_RETRY_MARGIN_MS",
    "COPY_LOCK_TTL_MS",
    "RECORDING_CONTENT_TYPE",
    "CopyLockAttempt",
    "RecordingActivities",
    "RecordingCopyLock",
    "copy_lock_key",
]

RECORDING_CONTENT_TYPE: Final = "audio/wav"
READY_MESSAGE: Final = "Recording stored."
FAILED_MESSAGE: Final = "The call recording could not be stored."
UPLOAD_FAILED_MESSAGE: Final = "The recording upload failed."
_SCRATCH_NAME: Final = "recording.wav"
BUSY_MESSAGE: Final = "Another recording of the call is being copied."
COPY_LOCK_TTL_MS: Final = int((STORE_RECORDING_TIMEOUT + timedelta(minutes=1)).total_seconds() * 1000)
"""The copy lock outlives one attempt (start-to-close timeout + 1 min), so a crashed holder frees it."""
BUSY_RETRY_MARGIN_MS: Final = 1000
"""A busy run retries this long after the lock's remaining TTL (also the delay when the lock has no TTL)."""
_TOO_MANY_REQUESTS: Final = 429
_SERVER_ERROR: Final = 500
_LINK_GONE: Final = frozenset({403, 404})
"""Download statuses of an expired or withdrawn presigned link: fetch fresh ``download_urls`` once."""
_DUAL_CHANNELS: Final = 2

_log = get_logger(__name__)


class _Failed(Exception):  # noqa: N818 - control flow carrying the failure detail
    def __init__(self, detail: RecordingFailure) -> None:
        super().__init__(detail)
        self.detail = detail


class _LinkGone(Exception):  # noqa: N818 - control flow: the presigned link answered 403/404
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


_ACQUIRE: Final = """
local current = redis.call('GET', KEYS[1])
if not current then
  redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
  return {1, 0}
end
if current == ARGV[1] then
  redis.call('PEXPIRE', KEYS[1], ARGV[2])
  return {1, 0}
end
return {0, redis.call('PTTL', KEYS[1])}
"""
_RELEASE: Final = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


def copy_lock_key(call_id: uuid.UUID) -> str:
    """``call:{<id>}:reccopy`` (specs/03 §11)."""
    return f"call:{{{call_id}}}:reccopy"


@dataclass(frozen=True, slots=True)
class CopyLockAttempt:
    """``taken``, else ``remaining_ms`` = the holder's remaining TTL (``PTTL``: ``-1`` without a TTL)."""

    taken: bool
    remaining_ms: int = 0

    def retry_delay(self) -> timedelta:
        """When a refused run should try again: after the holder's lock expires (+ a margin)."""
        return timedelta(milliseconds=max(self.remaining_ms, 0) + BUSY_RETRY_MARGIN_MS)


class RecordingCopyLock:
    """The per-call copy lock: free or held by the same owner → taken (TTL refreshed); else refused with the
    remaining TTL. Release deletes it only for its owner. Redis errors propagate."""

    def __init__(self, redis: Redis) -> None:
        self._acquire = redis.register_script(_ACQUIRE)
        self._release = redis.register_script(_RELEASE)

    async def acquire(self, call_id: uuid.UUID, owner: str) -> CopyLockAttempt:
        taken, remaining_ms = await self._acquire(
            keys=[copy_lock_key(call_id)], args=[owner, COPY_LOCK_TTL_MS]
        )
        return CopyLockAttempt(taken=bool(taken), remaining_ms=int(remaining_ms))

    async def release(self, call_id: uuid.UUID, owner: str) -> None:
        await self._release(keys=[copy_lock_key(call_id)], args=[owner])


@dataclass(frozen=True, slots=True)
class _StoredKey:
    """The call's recording key and duration (``None`` when unset or the call row vanished)."""

    key: str | None
    duration_ms: int | None


@dataclass(frozen=True, slots=True)
class _CallFacts:
    """What the copy needs from the ``calls`` row, read in a short transaction."""

    config: dict[str, JsonValue]
    created_at: datetime
    answered_at: datetime | None
    recording_s3_key: str | None
    recording_duration_ms: int | None

    def stored(self) -> _StoredKey:
        return _StoredKey(self.recording_s3_key, self.recording_duration_ms)


@dataclass(frozen=True, slots=True)
class _WavInfo:
    duration_ms: int
    channels: str


def _parse(inp: StoreRecordingInput) -> RecordingSaved:
    try:
        return RecordingSaved.model_validate(inp.event.payload)
    except ValidationError as exc:
        raise _Failed("invalid_payload") from exc


def _wav_url(saved: RecordingSaved) -> str:
    url = saved.recording_urls.wav or saved.public_recording_urls.wav
    if not url:
        raise _Failed("no_wav_url")
    return url


def _owner(inp: StoreRecordingInput, saved: RecordingSaved) -> str:
    """The recording's identity: ``recording_id``, else the event id (redeliveries share it)."""
    return f"rec:{saved.recording_id}" if saved.recording_id else f"evt:{inp.event.telnyx_event_id}"


def _telnyx_retryable(exc: TelnyxError) -> bool:
    return isinstance(exc, TelnyxRateLimited | TelnyxTransportError) or (
        isinstance(exc, TelnyxApiError) and (exc.status == _TOO_MANY_REQUESTS or exc.status >= _SERVER_ERROR)
    )


def _offset_ms(started_at: datetime | None, answered_at: datetime | None) -> int | None:
    """Milliseconds from the answer to the recording start (``None`` when either is unknown)."""
    if started_at is None or answered_at is None:
        return None
    return round((started_at - answered_at).total_seconds() * 1000)


def _read_wav(path: Path, declared: str | None) -> _WavInfo:
    """Duration and channel layout of the WAV at ``path`` (``_Failed("invalid_wav")`` when unreadable)."""
    try:
        with wave.open(str(path), "rb") as reader:
            frames, rate, channels = reader.getnframes(), reader.getframerate(), reader.getnchannels()
    except (wave.Error, EOFError) as exc:
        raise _Failed("invalid_wav") from exc
    if rate <= 0:
        raise _Failed("invalid_wav")
    layout = declared or ("dual" if channels == _DUAL_CHANNELS else "single")
    return _WavInfo(duration_ms=frames * 1000 // rate, channels=layout)


@asynccontextmanager
async def _scratch(parent: Path | None) -> AsyncIterator[Path]:
    """A private scratch directory, removed (with the download) on exit."""
    directory = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="va-recording-", dir=parent))
    try:
        yield directory
    finally:
        await asyncio.to_thread(shutil.rmtree, directory, ignore_errors=True)


class RecordingActivities:
    """Copies call recordings to object storage; one instance per worker process."""

    def __init__(  # noqa: PLR0913 - the activity collaborators
        self,
        *,
        database: Database,
        telnyx: TelnyxClientFactory,
        store: ObjectStore,
        bucket: str,
        sse: ServerSideEncryption | None,
        publisher: EventPublisher,
        org_settings: OrgSettingsReader,
        copy_lock: RecordingCopyLock,
        clock: Clock,
        tmp_dir: Path | None = None,
        max_download_bytes: int = MAX_DOWNLOAD_BYTES,
    ) -> None:
        self._database = database
        self._telnyx = telnyx
        self._store = store
        self._bucket = bucket
        self._sse = sse
        self._publisher = publisher
        self._org_settings = org_settings
        self._copy_lock = copy_lock
        self._clock = clock
        self._tmp_dir = tmp_dir
        self._max_download_bytes = max_download_bytes

    @activity.defn(name="store_recording")
    async def store_recording(self, inp: StoreRecordingInput) -> StoreRecordingResult:
        with bound_context(org_id=str(inp.org_id), call_id=str(inp.call_id)):
            try:
                return await self._store_recording(inp)
            except _Failed as failed:
                return await self._failed(inp, failed.detail)

    async def _store_recording(self, inp: StoreRecordingInput) -> StoreRecordingResult:
        call = await self._load(inp)
        if call is None:
            raise _Failed("call_not_found")
        if call.recording_s3_key is not None:
            return self._already_stored(call.stored())
        saved = _parse(inp)
        url = _wav_url(saved)
        if self._late(inp):
            _log.warning("recording.out_of_time", stage="start")
            raise recording_out_of_time()
        owner = _owner(inp, saved)
        locked = await self._lock(inp, owner)
        try:
            return await self._copy(inp, saved, url, owner)
        finally:
            if locked:
                await self._unlock(inp, owner)

    async def _copy(
        self, inp: StoreRecordingInput, saved: RecordingSaved, url: str, owner: str
    ) -> StoreRecordingResult:
        """The copy itself, under the lock; the key is read again because a previous holder may have just
        committed (this copy would then be wasted work)."""
        call = await self._load(inp)
        if call is None:
            raise _Failed("call_not_found")
        if call.recording_s3_key is not None:
            return self._already_stored(call.stored(), saved)
        delete_carrier_copy = await self._wants_carrier_delete(inp.org_id)
        key = recording_copy_key(inp.org_id, inp.call_id, call.created_at, owner)
        async with _scratch(self._tmp_dir) as directory:
            path = directory / _SCRATCH_NAME
            size = await self._fetch(call, saved, url, path)
            info = await asyncio.to_thread(_read_wav, path, saved.channels)
            await self._upload(inp, key, path)
        if self._late(inp):
            await self._delete_object(key)
            _log.warning("recording.out_of_time", stage="commit")
            raise recording_out_of_time()
        offset = _offset_ms(saved.recording_started_at, call.answered_at)
        stored = await self._record(inp, key, info, offset)
        if stored is not None:
            return await self._lost(key, stored, saved)
        _log.info("recording.stored", duration_ms=info.duration_ms, bytes=size)
        if delete_carrier_copy and saved.recording_id:
            await self._delete_carrier_copy(call, saved.recording_id)
        return StoreRecordingResult(
            status="stored", s3_key=key, duration_ms=info.duration_ms, recording_offset_ms=offset
        )

    def _late(self, inp: StoreRecordingInput) -> bool:
        """Too close to (or past) the workflow's ``commit_by`` to start or commit the copy."""
        return inp.commit_by is not None and self._clock.now() + COMMIT_MARGIN > inp.commit_by

    async def _lost(self, key: str, stored: _StoredKey, saved: RecordingSaved) -> StoreRecordingResult:
        """Another copy set the call's key while this one uploaded: delete this run's object unless it is the
        stored one (the same recording copied twice), and keep the carrier copy."""
        if stored.key != key:
            await self._delete_object(key)
        return self._already_stored(stored, saved)

    async def _delete_object(self, key: str) -> None:
        try:
            await self._store.delete(self._bucket, key)
        except (BotoCoreError, ClientError) as exc:
            _log.warning("recording.orphan_delete_failed", error=type(exc).__name__)
            return
        _log.info("recording.orphan_deleted")

    @staticmethod
    def _already_stored(stored: _StoredKey, saved: RecordingSaved | None = None) -> StoreRecordingResult:
        """The call has its recording; this one is not copied and its carrier copy is kept (it may be a
        second recording of the call, and a recording that was not copied is never deleted)."""
        recording_id = saved.recording_id if saved is not None else None
        _log.info("recording.already_stored", recording_id=recording_id)
        return StoreRecordingResult(
            status="already_stored", s3_key=stored.key, duration_ms=stored.duration_ms
        )

    async def _lock(self, inp: StoreRecordingInput, owner: str) -> bool:
        """Take the copy lock: ``True`` when held, ``False`` when Redis is unavailable (the copy proceeds
        unlocked: per-recording keys keep object and row in step, and an outage must not cost the copy)."""
        try:
            attempt = await self._copy_lock.acquire(inp.call_id, owner)
        except (RedisError, OSError) as exc:
            _log.warning("recording.lock_unavailable", error=type(exc).__name__)
            return False
        if not attempt.taken:
            delay = attempt.retry_delay()
            _log.info("recording.copy_busy", retry_in_ms=delay // timedelta(milliseconds=1))
            raise retryable(BUSY_MESSAGE, RECORDING_BUSY, next_retry_delay=delay)
        return True

    async def _unlock(self, inp: StoreRecordingInput, owner: str) -> None:
        """Release the lock; a failure is logged only (the TTL frees it)."""
        try:
            await self._copy_lock.release(inp.call_id, owner)
        except (RedisError, OSError) as exc:
            _log.warning("recording.unlock_failed", error=type(exc).__name__)

    async def _load(self, inp: StoreRecordingInput) -> _CallFacts | None:
        async with self._database.tenant_session(inp.org_id) as session:
            row = (
                await session.execute(
                    select(
                        Call.config,
                        Call.created_at,
                        Call.answered_at,
                        Call.recording_s3_key,
                        Call.recording_duration_ms,
                    ).where(Call.id == inp.call_id, Call.org_id == inp.org_id)
                )
            ).first()
        if row is None:
            return None
        config, created_at, answered_at, s3_key, duration_ms = row
        return _CallFacts(config, created_at, answered_at, s3_key, duration_ms)

    async def _wants_carrier_delete(self, org_id: uuid.UUID) -> bool:
        settings = await self._org_settings.get(org_id)
        return settings is None or settings.delete_carrier_recordings

    async def _fetch(self, call: _CallFacts, saved: RecordingSaved, url: str, path: Path) -> int:
        """Download ``url``; when its link is gone (403/404) and the event names the recording, download the
        fresh ``download_urls.wav`` once instead."""
        try:
            return await self._download(url, path)
        except _LinkGone as gone:
            if not saved.recording_id:
                _log.warning("recording.download_rejected", status=gone.status)
                raise _Failed("download_failed") from gone
        fresh = await self._fresh_url(call, saved.recording_id)
        _log.info("recording.url_refreshed")
        try:
            return await self._download(fresh, path)
        except _LinkGone as gone:
            _log.warning("recording.download_rejected", status=gone.status, refreshed=True)
            raise _Failed("download_failed") from gone

    async def _download(self, url: str, path: Path) -> int:
        try:
            return await self._telnyx.platform.download(url, path, max_bytes=self._max_download_bytes)
        except TelnyxDownloadTooLarge as exc:
            raise _Failed("too_large") from exc
        except TelnyxError as exc:
            if _telnyx_retryable(exc):
                _log.warning("recording.download_retry", error=type(exc).__name__)
                raise retryable(str(exc), TELNYX_UNAVAILABLE) from exc
            status = exc.status if isinstance(exc, TelnyxApiError) else None
            if status in _LINK_GONE:
                raise _LinkGone(status) from exc
            _log.warning("recording.download_rejected", status=status)
            raise _Failed("download_failed") from exc

    async def _fresh_url(self, call: _CallFacts, recording_id: str) -> str:
        """``download_urls.wav`` of ``GET /v2/recordings/{id}`` with the call's account."""
        try:
            client: TelnyxClient = await self._telnyx.for_call(call)
            recording = await client.get_recording(recording_id)
        except (UnknownTelnyxAccount, ValueError) as exc:
            _log.warning("recording.refresh_failed", error=type(exc).__name__)
            raise _Failed("download_failed") from exc
        except TelnyxError as exc:
            if _telnyx_retryable(exc):
                _log.warning("recording.refresh_retry", error=type(exc).__name__)
                raise retryable(str(exc), TELNYX_UNAVAILABLE) from exc
            status = exc.status if isinstance(exc, TelnyxApiError) else None
            _log.warning("recording.refresh_failed", error=type(exc).__name__, status=status)
            raise _Failed("download_failed") from exc
        if not recording.download_urls.wav:
            _log.warning("recording.refresh_failed", error="no_wav_url")
            raise _Failed("download_failed")
        return recording.download_urls.wav

    async def _upload(self, inp: StoreRecordingInput, key: str, path: Path) -> None:
        try:
            await self._store.upload_file(
                self._bucket,
                key,
                path,
                content_type=RECORDING_CONTENT_TYPE,
                metadata={"call_id": str(inp.call_id), "org_id": str(inp.org_id)},
                sse=self._sse,
            )
        except (BotoCoreError, ClientError) as exc:
            _log.warning("recording.upload_retry", error=type(exc).__name__)
            raise retryable(UPLOAD_FAILED_MESSAGE, STORAGE_UNAVAILABLE) from exc

    async def _record(
        self, inp: StoreRecordingInput, key: str, info: _WavInfo, offset_ms: int | None
    ) -> _StoredKey | None:
        """Set the key and duration and record ``call.recording_ready``; when a key was set meanwhile, that
        key (and duration) instead."""
        events: list[DomainEvent] = []
        async with self._database.tenant_session(inp.org_id) as session:
            result = await session.execute(
                update(Call)
                .where(Call.id == inp.call_id, Call.org_id == inp.org_id, Call.recording_s3_key.is_(None))
                .values(recording_s3_key=key, recording_duration_ms=info.duration_ms)
                .returning(Call.id)
            )
            if result.first() is None:
                current = (
                    await session.execute(
                        select(Call.recording_s3_key, Call.recording_duration_ms).where(
                            Call.id == inp.call_id, Call.org_id == inp.org_id
                        )
                    )
                ).first()
                if current is None:
                    return _StoredKey(None, None)
                return _StoredKey(current[0], current[1])
            data: dict[str, JsonValue] = {
                "duration_ms": info.duration_ms,
                "channels": info.channels,
                "recording_offset_ms": offset_ms,
            }
            event = call_event(
                self._clock,
                inp.org_id,
                inp.call_id,
                EventType.CALL_RECORDING_READY,
                message=READY_MESSAGE,
                data=data,
            )
            events = await persist_events(session, self._publisher, [event])
        await broadcast_events(self._publisher, events)
        return None

    async def _delete_carrier_copy(self, call: _CallFacts, recording_id: str) -> None:
        try:
            client = await self._telnyx.for_call(call)
            deleted = await client.delete_recording(recording_id)
        except (TelnyxError, UnknownTelnyxAccount, ValueError) as exc:
            _log.warning("recording.carrier_delete_failed", error=type(exc).__name__)
            return
        _log.info("recording.carrier_deleted", deleted=deleted)

    async def _failed(self, inp: StoreRecordingInput, detail: RecordingFailure) -> StoreRecordingResult:
        """Record ``recording_failed``, unless another copy stored the call's recording meanwhile (its
        carrier-copy delete is a likely cause of this run's failed download)."""
        if detail != "call_not_found":
            call = await self._load(inp)
            if call is not None and call.recording_s3_key is not None:
                _log.info("recording.failed_after_stored", detail=detail)
                return self._already_stored(call.stored())
        _log.warning("recording.store_failed", detail=detail)
        if detail != "call_not_found":
            data: dict[str, JsonValue] = {"code": "recording_failed", "detail": detail}
            event = call_event(
                self._clock,
                inp.org_id,
                inp.call_id,
                EventType.CALL_ERROR,
                message=FAILED_MESSAGE,
                data=data,
                level="warning",
            )
            async with self._database.tenant_session(inp.org_id) as session:
                events = await persist_events(session, self._publisher, [event])
            await broadcast_events(self._publisher, events)
        return StoreRecordingResult(status="failed", detail=detail)
