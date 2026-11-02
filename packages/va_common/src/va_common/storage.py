"""Object storage (S3 in AWS, MinIO in development) and recording keys (specs/02 §6 ``S3_*``;
specs/06-telephony-telnyx.md §11 Decision P05-T14).

:func:`open_object_store` opens one aiobotocore S3 client with bounded timeouts and retries and yields an
:class:`ObjectStore` over it. An empty ``S3_ENDPOINT_URL`` means AWS (the default endpoint, path-style off);
a non-empty one is MinIO (path-style addressing). Static credentials are passed only when both are set;
otherwise the default credential chain applies (IRSA in AWS).

Recordings are encrypted at rest with SSE-KMS under ``AWS_KMS_KEY_ID`` when ``KMS_PROVIDER=aws``
(:func:`sse_for`); the local provider (MinIO in development and tests) sends no SSE header because the
development MinIO has no KMS configured.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

import aioboto3
from aiobotocore.config import AioConfig
from botocore.exceptions import ClientError
from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict

from va_common.settings import BaseServiceSettings, CryptoSettings, KmsProviderName

if TYPE_CHECKING:
    from types_aiobotocore_s3 import S3Client

__all__ = [
    "MAX_PRESIGN_TTL_S",
    "PLAYBACK_URL_TTL_S",
    "RECORDING_URL_TTL_S",
    "ObjectStore",
    "RecordingVariant",
    "S3Settings",
    "ServerSideEncryption",
    "SignedUrl",
    "open_object_store",
    "recording_key",
    "recording_variant_key",
    "signed_url",
    "sse_for",
]

RECORDING_URL_TTL_S: Final = 7 * 86_400
"""Lifetime of the Call Object ``recording_url`` (7 days)."""
PLAYBACK_URL_TTL_S: Final = 900
"""Lifetime of a recording playback URL (specs/05 §7.5)."""
MAX_PRESIGN_TTL_S: Final = 604_800
"""SigV4 presigned URLs live at most 7 days."""

_NOT_FOUND_CODES: Final = frozenset({"404", "NoSuchKey", "NotFound"})

type RecordingVariant = Literal["mixed", "caller", "agent"]


class S3Settings(BaseServiceSettings):
    """``S3_*`` (api, worker, media). Validation errors never echo the input (the secret key)."""

    model_config = SettingsConfigDict(hide_input_in_errors=True)

    s3_endpoint_url: str = "http://localhost:9000"
    s3_region: str = "us-east-1"
    s3_bucket_recordings: str = "va-recordings"
    s3_bucket_kb: str = "va-kb"
    s3_bucket_voices: str = "va-voice-samples"
    s3_access_key_id: str = ""
    s3_secret_access_key: SecretStr = SecretStr("")


@dataclass(frozen=True, slots=True)
class ServerSideEncryption:
    """The SSE header set of an upload: SSE-KMS under ``kms_key_id`` or SSE-S3 (``AES256``)."""

    kind: Literal["aws:kms", "AES256"]
    kms_key_id: str | None = None

    def extra_args(self) -> dict[str, str]:
        args: dict[str, str] = {"ServerSideEncryption": self.kind}
        if self.kind == "aws:kms" and self.kms_key_id:
            args["SSEKMSKeyId"] = self.kms_key_id
        return args


@dataclass(frozen=True, slots=True)
class SignedUrl:
    """A presigned GET URL and the moment it stops working."""

    url: str
    expires_at: datetime


def sse_for(crypto: CryptoSettings) -> ServerSideEncryption | None:
    """SSE-KMS under ``AWS_KMS_KEY_ID`` with the AWS provider; no SSE header with the local provider."""
    if crypto.kms_provider is KmsProviderName.AWS:
        return ServerSideEncryption("aws:kms", crypto.aws_kms_key_id)
    return None


def _month_prefix(org_id: uuid.UUID, created_at: datetime) -> str:
    if created_at.tzinfo is None:
        msg = "created_at must be timezone-aware"
        raise ValueError(msg)
    utc = created_at.astimezone(UTC)
    return f"{org_id}/{utc:%Y}/{utc:%m}"


def recording_key(org_id: uuid.UUID, call_id: uuid.UUID, created_at: datetime) -> str:
    """``{org_id}/{yyyy}/{mm}/{call_id}.wav`` (UTC month of the call's ``created_at``)."""
    return f"{_month_prefix(org_id, created_at)}/{call_id}.wav"


def recording_variant_key(
    org_id: uuid.UUID, call_id: uuid.UUID, created_at: datetime, variant: RecordingVariant
) -> str:
    """``{org_id}/{yyyy}/{mm}/{call_id}/{mixed|caller|agent}.wav`` (post-call variants)."""
    return f"{_month_prefix(org_id, created_at)}/{call_id}/{variant}.wav"


def _client_error_code(exc: ClientError) -> str:
    error = exc.response.get("Error", {})
    code = error.get("Code") if isinstance(error, Mapping) else None
    return str(code) if code is not None else ""


class ObjectStore:
    """Uploads, existence checks, deletes, and presigned GET URLs on one S3 client."""

    def __init__(self, client: S3Client) -> None:
        self._client = client

    def __repr__(self) -> str:
        return "ObjectStore()"

    async def upload_file(  # noqa: PLR0913 - one keyword per object attribute
        self,
        bucket: str,
        key: str,
        path: Path,
        *,
        content_type: str,
        metadata: Mapping[str, str],
        sse: ServerSideEncryption | None,
    ) -> None:
        """Upload ``path`` (multipart for large files) with its content type, metadata, and SSE headers."""
        extra: dict[str, Any] = {"ContentType": content_type, "Metadata": dict(metadata)}
        if sse is not None:
            extra.update(sse.extra_args())
        await self._client.upload_file(str(path), bucket, key, ExtraArgs=extra)

    async def exists(self, bucket: str, key: str) -> bool:
        """Whether ``key`` exists; any error other than not-found propagates."""
        try:
            await self._client.head_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            if _client_error_code(exc) in _NOT_FOUND_CODES:
                return False
            raise
        return True

    async def delete(self, bucket: str, key: str) -> None:
        """Delete ``key`` (deleting an absent key succeeds)."""
        await self._client.delete_object(Bucket=bucket, Key=key)

    async def presign_get(self, bucket: str, key: str, *, expires_s: int) -> str:
        """A presigned GET URL valid for ``expires_s`` seconds (1 to 604 800, the SigV4 limit)."""
        if not 1 <= expires_s <= MAX_PRESIGN_TTL_S:
            msg = f"expires_s must be between 1 and {MAX_PRESIGN_TTL_S}, got {expires_s}"
            raise ValueError(msg)
        return await self._client.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires_s
        )


async def signed_url(store: ObjectStore, bucket: str, key: str, *, now: datetime, ttl_s: int) -> SignedUrl:
    """A presigned GET URL for ``key`` and its expiry (``now + ttl_s``)."""
    url = await store.presign_get(bucket, key, expires_s=ttl_s)
    return SignedUrl(url, now + timedelta(seconds=ttl_s))


def _client_config(settings: S3Settings) -> AioConfig:
    s3_options: dict[str, Any] = {"addressing_style": "path"} if settings.s3_endpoint_url else {}
    return AioConfig(
        connect_timeout=5,
        read_timeout=60,
        retries={"max_attempts": 3, "mode": "standard"},
        signature_version="s3v4",
        s3=s3_options,
    )


@asynccontextmanager
async def open_object_store(
    settings: S3Settings, *, session: aioboto3.Session | None = None
) -> AsyncIterator[ObjectStore]:
    """Open the S3 client described by ``settings`` and yield a store over it (closed on exit)."""
    s3_session = session if session is not None else aioboto3.Session()
    kwargs: dict[str, Any] = {"region_name": settings.s3_region, "config": _client_config(settings)}
    if settings.s3_endpoint_url:
        kwargs["endpoint_url"] = settings.s3_endpoint_url
    secret = settings.s3_secret_access_key.get_secret_value()
    if settings.s3_access_key_id and secret:
        kwargs["aws_access_key_id"] = settings.s3_access_key_id
        kwargs["aws_secret_access_key"] = secret
    async with s3_session.client("s3", **kwargs) as client:
        yield ObjectStore(client)
