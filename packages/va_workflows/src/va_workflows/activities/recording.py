"""``store_recording``: copy a Telnyx recording to object storage (specs/18-post-call-analysis.md §1 step 3;
specs/06-telephony-telnyx.md §11 Decision P05-T14).

The activity takes the ``call.recording.saved`` carrier event, downloads ``recording_urls.wav`` (else
``public_recording_urls.wav``; an mp3 is never copied because the platform always records WAV) to a scratch
file, reads the duration with the stdlib ``wave`` module, uploads the file to
``{S3_BUCKET_RECORDINGS}/{org_id}/{yyyy}/{mm}/{call_id}.wav`` (metadata ``call_id``/``org_id``, SSE-KMS
with the AWS KMS provider), and in one transaction sets ``calls.recording_s3_key``/``recording_duration_ms``
(guarded by ``recording_s3_key IS NULL``) and records ``call.recording_ready``. After the commit it deletes
the carrier copy when the org's ``delete_carrier_recordings`` (default true) says so; a failed delete is
logged only.

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
from datetime import datetime
from pathlib import Path
from typing import Final

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import JsonValue, ValidationError
from sqlalchemy import select, update
from temporalio import activity

from va_common.logging import bound_context, get_logger
from va_common.storage import ObjectStore, ServerSideEncryption, recording_key
from va_common.time import Clock
from va_db.models import Call
from va_db.org_settings import OrgSettingsReader
from va_db.session import Database
from va_events import DomainEvent, EventPublisher, EventType
from va_telnyx import (
    TelnyxApiError,
    TelnyxClientFactory,
    TelnyxDownloadTooLarge,
    TelnyxError,
    TelnyxRateLimited,
    TelnyxTransportError,
    UnknownTelnyxAccount,
)
from va_telnyx.messaging import MAX_DOWNLOAD_BYTES
from va_telnyx.voice_events import RecordingSaved
from va_workflows.activities.errors import STORAGE_UNAVAILABLE, TELNYX_UNAVAILABLE, retryable
from va_workflows.activities.events import broadcast_events, call_event, persist_events
from va_workflows.activities.recording_inputs import (
    RecordingFailure,
    StoreRecordingInput,
    StoreRecordingResult,
)

__all__ = ["RECORDING_CONTENT_TYPE", "RecordingActivities"]

RECORDING_CONTENT_TYPE: Final = "audio/wav"
READY_MESSAGE: Final = "Recording stored."
FAILED_MESSAGE: Final = "The call recording could not be stored."
UPLOAD_FAILED_MESSAGE: Final = "The recording upload failed."
_SCRATCH_NAME: Final = "recording.wav"
_TOO_MANY_REQUESTS: Final = 429
_SERVER_ERROR: Final = 500
_DUAL_CHANNELS: Final = 2

_log = get_logger(__name__)


class _Failed(Exception):  # noqa: N818 - control flow carrying the failure detail
    def __init__(self, detail: RecordingFailure) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True, slots=True)
class _CallFacts:
    """What the copy needs from the ``calls`` row, read in a short transaction."""

    config: dict[str, JsonValue]
    created_at: datetime
    answered_at: datetime | None
    recording_s3_key: str | None
    recording_duration_ms: int | None


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
            _log.info("recording.already_stored")
            return StoreRecordingResult(
                status="already_stored", s3_key=call.recording_s3_key, duration_ms=call.recording_duration_ms
            )
        saved = _parse(inp)
        url = _wav_url(saved)
        delete_carrier_copy = await self._wants_carrier_delete(inp.org_id)
        key = recording_key(inp.org_id, inp.call_id, call.created_at)
        async with _scratch(self._tmp_dir) as directory:
            path = directory / _SCRATCH_NAME
            size = await self._download(url, path)
            info = await asyncio.to_thread(_read_wav, path, saved.channels)
            await self._upload(inp, key, path)
        offset = _offset_ms(saved.recording_started_at, call.answered_at)
        if not await self._record(inp, key, info, offset):
            _log.info("recording.already_stored")
            return StoreRecordingResult(status="already_stored")
        _log.info("recording.stored", duration_ms=info.duration_ms, bytes=size)
        if delete_carrier_copy and saved.recording_id:
            await self._delete_carrier_copy(call, saved.recording_id)
        return StoreRecordingResult(
            status="stored", s3_key=key, duration_ms=info.duration_ms, recording_offset_ms=offset
        )

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

    async def _download(self, url: str, path: Path) -> int:
        try:
            return await self._telnyx.platform.download(url, path, max_bytes=self._max_download_bytes)
        except TelnyxDownloadTooLarge as exc:
            raise _Failed("too_large") from exc
        except (TelnyxRateLimited, TelnyxTransportError) as exc:
            _log.warning("recording.download_retry", error=type(exc).__name__)
            raise retryable(str(exc), TELNYX_UNAVAILABLE) from exc
        except TelnyxApiError as exc:
            if exc.status == _TOO_MANY_REQUESTS or exc.status >= _SERVER_ERROR:
                _log.warning("recording.download_retry", error=type(exc).__name__, status=exc.status)
                raise retryable(str(exc), TELNYX_UNAVAILABLE) from exc
            _log.warning("recording.download_rejected", status=exc.status)
            raise _Failed("download_failed") from exc

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
    ) -> bool:
        """Set the key and duration and record ``call.recording_ready``; ``False`` when a key was set
        meanwhile."""
        events: list[DomainEvent] = []
        async with self._database.tenant_session(inp.org_id) as session:
            result = await session.execute(
                update(Call)
                .where(Call.id == inp.call_id, Call.org_id == inp.org_id, Call.recording_s3_key.is_(None))
                .values(recording_s3_key=key, recording_duration_ms=info.duration_ms)
                .returning(Call.id)
            )
            if result.first() is None:
                return False
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
        return True

    async def _delete_carrier_copy(self, call: _CallFacts, recording_id: str) -> None:
        try:
            client = await self._telnyx.for_call(call)
            deleted = await client.delete_recording(recording_id)
        except (TelnyxError, UnknownTelnyxAccount, ValueError) as exc:
            _log.warning("recording.carrier_delete_failed", error=type(exc).__name__)
            return
        _log.info("recording.carrier_deleted", deleted=deleted)

    async def _failed(self, inp: StoreRecordingInput, detail: RecordingFailure) -> StoreRecordingResult:
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
