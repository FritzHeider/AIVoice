"""Object store helpers: keys, SSE, presign bounds, settings (specs/06 §11 Decision P05-T14)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from botocore.exceptions import ClientError
from pydantic import SecretStr, ValidationError
from service_helpers import isolate_env, settings_of

from va_common.settings import CryptoSettings, KmsProviderName
from va_common.storage import (
    MAX_PRESIGN_TTL_S,
    ObjectStore,
    S3Settings,
    ServerSideEncryption,
    _client_config,
    open_object_store,
    recording_key,
    recording_variant_key,
    signed_url,
    sse_for,
)

if TYPE_CHECKING:
    from types_aiobotocore_s3 import S3Client

ORG = uuid.UUID("018f0000-0000-7000-8000-000000000001")
CALL = uuid.UUID("018f0000-0000-7000-8000-0000000000aa")


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    isolate_env(monkeypatch, tmp_path, S3Settings, CryptoSettings)


class FakeS3:
    """Records the S3 calls the store makes; ``head_error`` makes ``head_object`` fail."""

    def __init__(self, head_error: ClientError | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.head_error = head_error

    async def upload_file(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("upload_file", args, kwargs))

    async def head_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("head_object", (), kwargs))
        if self.head_error is not None:
            raise self.head_error
        return {}

    async def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("delete_object", (), kwargs))
        return {}

    async def generate_presigned_url(self, method: str, **kwargs: Any) -> str:
        self.calls.append(("generate_presigned_url", (method,), kwargs))
        return f"https://s3.test/{kwargs['Params']['Key']}?X-Amz-Expires={kwargs['ExpiresIn']}"


def _store(fake: FakeS3) -> ObjectStore:
    return ObjectStore(cast("S3Client", fake))


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "m"}}, "HeadObject")


def test_recording_key_uses_the_utc_month_of_created_at() -> None:
    created = datetime(2026, 11, 1, 1, 30, tzinfo=timezone(timedelta(hours=5)))  # 2026-10-31 20:30 UTC
    assert recording_key(ORG, CALL, created) == f"{ORG}/2026/10/{CALL}.wav"
    assert recording_key(ORG, CALL, datetime(2026, 3, 9, tzinfo=UTC)) == f"{ORG}/2026/03/{CALL}.wav"


def test_variant_keys_sit_under_the_call_directory() -> None:
    created = datetime(2026, 12, 31, 23, 59, tzinfo=UTC)
    assert recording_variant_key(ORG, CALL, created, "mixed") == f"{ORG}/2026/12/{CALL}/mixed.wav"
    assert recording_variant_key(ORG, CALL, created, "caller") == f"{ORG}/2026/12/{CALL}/caller.wav"


def test_naive_created_at_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        recording_key(ORG, CALL, datetime(2026, 1, 1))  # noqa: DTZ001 - the refused input


def test_sse_is_kms_with_the_aws_provider_and_absent_locally() -> None:
    aws = settings_of(CryptoSettings, kms_provider=KmsProviderName.AWS, aws_kms_key_id="alias/va")
    assert sse_for(aws) == ServerSideEncryption("aws:kms", "alias/va")
    assert sse_for(settings_of(CryptoSettings)) is None


def test_sse_extra_args() -> None:
    assert ServerSideEncryption("aws:kms", "k1").extra_args() == {
        "ServerSideEncryption": "aws:kms",
        "SSEKMSKeyId": "k1",
    }
    assert ServerSideEncryption("AES256").extra_args() == {"ServerSideEncryption": "AES256"}


async def test_upload_sends_content_type_metadata_and_sse_only_when_set(tmp_path: Path) -> None:
    fake = FakeS3()
    store = _store(fake)
    path = tmp_path / "a.wav"
    path.write_bytes(b"RIFF")
    await store.upload_file("b", "k", path, content_type="audio/wav", metadata={"call_id": "c"}, sse=None)
    await store.upload_file(
        "b", "k", path, content_type="audio/wav", metadata={}, sse=ServerSideEncryption("aws:kms", "kid")
    )
    (_, args1, kw1), (_, _, kw2) = fake.calls
    assert args1 == (str(path), "b", "k")
    assert kw1 == {"ExtraArgs": {"ContentType": "audio/wav", "Metadata": {"call_id": "c"}}}
    assert kw2["ExtraArgs"]["ServerSideEncryption"] == "aws:kms"
    assert kw2["ExtraArgs"]["SSEKMSKeyId"] == "kid"


async def test_exists_maps_not_found_and_raises_other_errors() -> None:
    assert await _store(FakeS3()).exists("b", "k") is True
    for code in ("404", "NoSuchKey", "NotFound"):
        assert await _store(FakeS3(_client_error(code))).exists("b", "k") is False
    with pytest.raises(ClientError):
        await _store(FakeS3(_client_error("403"))).exists("b", "k")
    bare = ClientError({}, "HeadObject")
    with pytest.raises(ClientError):
        await _store(FakeS3(bare)).exists("b", "k")


async def test_delete_and_presign_call_the_client() -> None:
    fake = FakeS3()
    store = _store(fake)
    await store.delete("b", "k")
    url = await store.presign_get("b", "k", expires_s=60)
    assert url == "https://s3.test/k?X-Amz-Expires=60"
    assert fake.calls == [
        ("delete_object", (), {"Bucket": "b", "Key": "k"}),
        ("generate_presigned_url", ("get_object",), {"Params": {"Bucket": "b", "Key": "k"}, "ExpiresIn": 60}),
    ]
    assert repr(store) == "ObjectStore()"


@pytest.mark.parametrize("ttl", [0, -1, MAX_PRESIGN_TTL_S + 1])
async def test_presign_rejects_lifetimes_outside_sigv4_bounds(ttl: int) -> None:
    with pytest.raises(ValueError, match="expires_s"):
        await _store(FakeS3()).presign_get("b", "k", expires_s=ttl)


async def test_signed_url_expires_after_the_ttl() -> None:
    now = datetime(2026, 11, 2, 12, tzinfo=UTC)
    signed = await signed_url(_store(FakeS3()), "b", "k", now=now, ttl_s=MAX_PRESIGN_TTL_S)
    assert signed.expires_at == now + timedelta(days=7)
    assert signed.url.endswith(f"X-Amz-Expires={MAX_PRESIGN_TTL_S}")


def test_settings_defaults_and_secret_handling() -> None:
    settings = settings_of(S3Settings)
    assert settings.s3_bucket_recordings == "va-recordings"
    assert settings.s3_bucket_kb == "va-kb"
    assert settings.s3_bucket_voices == "va-voice-samples"
    configured = settings_of(S3Settings, s3_secret_access_key=SecretStr("very-secret-value"))
    assert "very-secret-value" not in repr(configured)
    with pytest.raises(ValidationError) as caught:
        settings_of(S3Settings, s3_region=["very-secret-value"])
    assert "very-secret-value" not in str(caught.value)


def test_client_config_is_path_style_only_with_a_custom_endpoint() -> None:
    minio = _client_config(settings_of(S3Settings, s3_endpoint_url="http://minio:9000"))
    aws = _client_config(settings_of(S3Settings, s3_endpoint_url=""))
    assert getattr(minio, "s3") == {"addressing_style": "path"}  # noqa: B009 - untyped botocore attribute
    assert not getattr(aws, "s3")  # noqa: B009
    assert getattr(minio, "signature_version") == "s3v4"  # noqa: B009


async def test_an_empty_endpoint_opens_the_default_aws_endpoint_without_static_credentials() -> None:
    settings = settings_of(S3Settings, s3_endpoint_url="", s3_region="eu-west-1")
    async with open_object_store(settings) as store:
        client = store._client
        assert client.meta.region_name == "eu-west-1"
        assert client.meta.endpoint_url == "https://s3.eu-west-1.amazonaws.com"
