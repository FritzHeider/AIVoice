"""``store_recording`` rig: the telephony rig (FakeTelnyx serves the recording URLs), an in-memory S3 client
behind a real :class:`ObjectStore`, and builders for recordings and ``call.recording.saved`` events."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

from botocore.exceptions import ClientError
from call_rows import SeededCall
from fakeredis import FakeAsyncRedis
from telephony_rig import Rig

from va_common.storage import ObjectStore, ServerSideEncryption
from va_db.org_settings import OrgSettingsReader
from va_domain.carrier import CarrierEvent
from va_sim.wav import pcm16_wav
from va_telnyx import TelnyxClientFactory
from va_workflows.activities.recording import RecordingActivities, RecordingCopyLock
from va_workflows.activities.recording_inputs import StoreRecordingInput

if TYPE_CHECKING:
    from types_aiobotocore_s3 import S3Client

__all__ = [
    "BUCKET",
    "PRESIGN_HOST",
    "SAMPLE_RATE",
    "STARTED_AT",
    "MemoryS3",
    "StoredObject",
    "activities",
    "recording_url",
    "saved_input",
    "serve_recording",
    "wav_bytes",
]

BUCKET: Final = "va-recordings-test"
PRESIGN_HOST: Final = "https://s3.test"
SAMPLE_RATE: Final = 8000
STARTED_AT: Final = datetime(2026, 11, 2, 12, 0, 1, tzinfo=UTC)

type UploadHook = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class StoredObject:
    body: bytes
    extra: dict[str, Any]


@dataclass(slots=True)
class MemoryS3:
    """The S3 calls :class:`ObjectStore` makes, kept in memory; ``fail_uploads`` uploads raise first,
    ``before_upload`` runs inside the next upload (to change the database mid-copy), ``deleted`` lists deleted
    keys (``fail_deletes`` refuses them)."""

    objects: dict[tuple[str, str], StoredObject] = field(default_factory=dict)
    uploads: int = 0
    fail_uploads: int = 0
    fail_presign: bool = False
    fail_deletes: bool = False
    before_upload: UploadHook | None = None
    deleted: list[str] = field(default_factory=list)

    async def upload_file(
        self,
        filename: str,
        bucket: str,
        key: str,
        ExtraArgs: dict[str, Any] | None = None,  # noqa: N803
    ) -> None:
        if self.before_upload is not None:
            hook, self.before_upload = self.before_upload, None
            await hook()
        if self.fail_uploads:
            self.fail_uploads -= 1
            raise ClientError({"Error": {"Code": "SlowDown", "Message": "slow down"}}, "PutObject")
        self.uploads += 1
        body = await asyncio.to_thread(Path(filename).read_bytes)
        self.objects[(bucket, key)] = StoredObject(body, dict(ExtraArgs or {}))

    async def delete_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if self.fail_deletes:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "DeleteObject")
        self.deleted.append(Key)
        self.objects.pop((Bucket, Key), None)
        return {}

    async def generate_presigned_url(
        self,
        operation: str,
        Params: dict[str, str],  # noqa: N803
        ExpiresIn: int,  # noqa: N803
    ) -> str:
        """``{PRESIGN_HOST}/{bucket}/{key}?op=<operation>&expires=<s>`` (``fail_presign`` raises)."""
        if self.fail_presign:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "GetObject")
        return f"{PRESIGN_HOST}/{Params['Bucket']}/{Params['Key']}?op={operation}&expires={ExpiresIn}"

    def store(self) -> ObjectStore:
        return ObjectStore(cast("S3Client", self))


def wav_bytes(seconds: float, *, channels: int = 2) -> bytes:
    """A PCM16 8 kHz WAV of ``seconds`` with a ramp on every channel."""
    frames = round(seconds * SAMPLE_RATE)
    track = b"".join((i % 256).to_bytes(2, "little", signed=True) for i in range(frames))
    return pcm16_wav([track] * channels, SAMPLE_RATE)


def recording_url(recording_id: str) -> str:
    return f"http://sim/sim/recordings/{recording_id}.wav"


def serve_recording(r: Rig, body: bytes) -> str:
    """Store ``body`` as a FakeTelnyx recording; its id."""
    recording_id = r.state.ids.uuid()
    r.state.store_recording(recording_id, body)
    return recording_id


def saved_input(
    call: SeededCall, recording_id: str | None, *, payload: dict[str, Any] | None = None, **fields_: Any
) -> StoreRecordingInput:
    """``store_recording``'s input for a ``call.recording.saved`` of ``call``; ``fields_`` (and ``payload``,
    for keys that clash with parameter names) override payload fields."""
    url = recording_url(recording_id) if recording_id is not None else None
    fields: dict[str, Any] = {
        "call_control_id": "v3:primary",
        "recording_id": recording_id,
        "channels": "dual",
        "format": "wav",
        "recording_started_at": STARTED_AT.isoformat(),
        "recording_ended_at": STARTED_AT.isoformat(),
        "recording_urls": {"wav": url, "mp3": None},
        "public_recording_urls": {"wav": url, "mp3": None},
        **fields_,
        **(payload or {}),
    }
    event = CarrierEvent(
        type="call.recording.saved",
        leg="primary",
        call_id=call.call_id,
        org_id=call.org_id,
        telnyx_event_id=f"evt-{uuid.uuid4()}",
        occurred_at=STARTED_AT,
        payload=fields,
    )
    return StoreRecordingInput(call_id=call.call_id, org_id=call.org_id, event=event)


def activities(  # noqa: PLR0913 - keyword-only test seams
    r: Rig,
    s3: MemoryS3,
    *,
    telnyx: TelnyxClientFactory | None = None,
    sse: ServerSideEncryption | None = None,
    tmp_dir: Path | None = None,
    max_download_bytes: int | None = None,
    copy_lock: RecordingCopyLock | None = None,
) -> RecordingActivities:
    extra: dict[str, Any] = {} if max_download_bytes is None else {"max_download_bytes": max_download_bytes}
    return RecordingActivities(
        database=r.database,
        telnyx=telnyx or r.factory,
        store=s3.store(),
        bucket=BUCKET,
        sse=sse,
        publisher=r.publisher,
        org_settings=OrgSettingsReader(r.database, FakeAsyncRedis()),
        copy_lock=copy_lock or RecordingCopyLock(FakeAsyncRedis()),
        clock=r.clock,
        tmp_dir=tmp_dir,
        **extra,
    )
