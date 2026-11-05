"""Input, result, timeout, and retry policy of ``store_recording`` (specs/18-post-call-analysis.md §1 step 3;
specs/06-telephony-telnyx.md §11 Decision P05-T14).

Workflows import this module to schedule the activity, so it imports only pydantic, the Temporal
``RetryPolicy``, the import-free carrier event model, and the error type names.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict
from temporalio.common import RetryPolicy

from va_domain.carrier import CarrierEvent

__all__ = [
    "COMMIT_MARGIN",
    "STORE_RECORDING_RETRY",
    "STORE_RECORDING_TIMEOUT",
    "RecordingFailure",
    "StoreRecordingInput",
    "StoreRecordingResult",
    "StoreRecordingStatus",
]

type StoreRecordingStatus = Literal["stored", "already_stored", "failed"]
"""``stored``: copied now; ``already_stored``: an earlier attempt (or another recording) set the key,
nothing was uploaded or emitted; ``failed``: the recording cannot be copied (``detail`` says why; a
``call.error`` ``recording_failed`` was recorded unless the call does not exist)."""

type RecordingFailure = Literal[
    "call_not_found", "invalid_payload", "no_wav_url", "download_failed", "too_large", "invalid_wav"
]

STORE_RECORDING_TIMEOUT: Final = timedelta(minutes=5)
STORE_RECORDING_RETRY: Final = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=5,
)


COMMIT_MARGIN: Final = timedelta(seconds=5)
"""An attempt starts and commits a copy only while ``now + COMMIT_MARGIN <= commit_by``: room for the key
``UPDATE`` and the event insert before the workflow's ``schedule_to_close`` deadline."""


class StoreRecordingInput(BaseModel):
    """The call and the ``call.recording.saved`` carrier event the edge signalled; ``commit_by`` is the copy
    deadline the workflow set (its ``schedule_to_close``), ``None`` = no deadline (specs/18 §1 Decision
    P08-FIX-7)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: uuid.UUID
    org_id: uuid.UUID
    event: CarrierEvent
    commit_by: datetime | None = None


class StoreRecordingResult(BaseModel):
    """What happened; ``s3_key``/``duration_ms`` describe the stored object."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: StoreRecordingStatus
    s3_key: str | None = None
    duration_ms: int | None = None
    recording_offset_ms: int | None = None
    detail: RecordingFailure | None = None
