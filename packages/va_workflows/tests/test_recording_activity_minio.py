"""``store_recording`` end to end: FakeTelnyx recording URL → real MinIO object (specs/06 §11 Decision
P05-T14)."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta

import httpx
import pytest
from call_rows import load_call
from fakeredis import FakeAsyncRedis
from minio_container import Minio, make_bucket, running_minio
from recording_rig import STARTED_AT, saved_input, serve_recording, wav_bytes
from telephony_rig import Rig, rig
from temporalio.testing import ActivityEnvironment

from va_common.storage import PLAYBACK_URL_TTL_S, open_object_store, signed_url
from va_db.org_settings import OrgSettingsReader
from va_db.session import Database
from va_workflows.activities.recording import RecordingActivities
from va_workflows.activities.recording_inputs import StoreRecordingResult

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def minio() -> Iterator[Minio]:
    with running_minio() as server:
        yield server


@pytest.fixture
async def r(database: Database) -> AsyncIterator[Rig]:
    async with rig(database) as built:
        yield built


async def test_the_recording_lands_in_minio_with_its_metadata(r: Rig, minio: Minio) -> None:
    bucket = f"va-rec-{secrets.token_hex(4)}"
    await make_bucket(minio, bucket)
    call = await r.seed("completed", answered_at=STARTED_AT - timedelta(seconds=1))
    body = wav_bytes(1.5)
    recording_id = serve_recording(r, body)
    async with open_object_store(minio.settings()) as store:
        activities = RecordingActivities(
            database=r.database,
            telnyx=r.factory,
            store=store,
            bucket=bucket,
            sse=None,
            publisher=r.publisher,
            org_settings=OrgSettingsReader(r.database, FakeAsyncRedis()),
            clock=r.clock,
        )
        result: StoreRecordingResult = await ActivityEnvironment().run(
            activities.store_recording, saved_input(call, recording_id)
        )
        assert (result.status, result.duration_ms) == ("stored", 1500)
        assert result.s3_key is not None
        assert result.s3_key == (await load_call(r.database, call)).recording_s3_key
        assert await store.exists(bucket, result.s3_key)
        signed = await signed_url(store, bucket, result.s3_key, now=r.clock.now(), ttl_s=PLAYBACK_URL_TTL_S)
    async with httpx.AsyncClient(timeout=10) as http:
        response = await http.get(signed.url)
    assert response.status_code == 200
    assert response.content == body
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["x-amz-meta-call_id"] == str(call.call_id)
    assert response.headers["x-amz-meta-org_id"] == str(call.org_id)
