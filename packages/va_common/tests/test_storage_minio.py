"""``ObjectStore`` against a real MinIO server (specs/06 §11 Decision P05-T14)."""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from botocore.exceptions import ClientError
from minio_container import Minio, make_bucket, running_minio
from pydantic import SecretStr

from va_common.storage import ObjectStore, open_object_store, signed_url

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def minio() -> Iterator[Minio]:
    with running_minio() as server:
        yield server


async def test_upload_presign_and_delete_round_trip(minio: Minio, tmp_path: Path) -> None:
    bucket = f"rec-{secrets.token_hex(4)}"
    await make_bucket(minio, bucket)
    payload = secrets.token_bytes(4096)
    path = tmp_path / "x.wav"
    path.write_bytes(payload)
    async with open_object_store(minio.settings()) as store:
        assert isinstance(store, ObjectStore)
        assert await store.exists(bucket, "o/2026/11/c.wav") is False
        await store.upload_file(
            bucket, "o/2026/11/c.wav", path, content_type="audio/wav", metadata={"call_id": "c"}, sse=None
        )
        assert await store.exists(bucket, "o/2026/11/c.wav") is True
        signed = await signed_url(store, bucket, "o/2026/11/c.wav", now=datetime.now(UTC), ttl_s=900)
        async with httpx.AsyncClient(timeout=10) as http:
            response = await http.get(signed.url)
        assert response.status_code == 200
        assert response.content == payload
        assert response.headers["content-type"] == "audio/wav"
        assert response.headers["x-amz-meta-call_id"] == "c"
        await store.delete(bucket, "o/2026/11/c.wav")
        assert await store.exists(bucket, "o/2026/11/c.wav") is False


async def test_wrong_credentials_fail_the_existence_check(minio: Minio) -> None:
    settings = minio.settings(s3_secret_access_key=SecretStr(secrets.token_hex(16)))
    async with open_object_store(settings) as store:
        with pytest.raises(ClientError, match=r"403|SignatureDoesNotMatch|Forbidden"):
            await store.exists("anything", "k")
