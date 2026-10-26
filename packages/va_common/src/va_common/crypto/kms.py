"""Key-encryption-key providers that wrap and unwrap per-org data keys (specs/28-security.md §3).

``LocalKmsProvider`` wraps with AES-256-GCM under ``LOCAL_MASTER_KEY`` (local and test only);
``AwsKmsProvider`` uses ``kms:GenerateDataKey`` / ``kms:Decrypt`` with the encryption context
``{"org_id": "<uuid>"}``. The local wrap binds the org the same way with ``org_id=<uuid>`` as GCM AAD.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol
from uuid import UUID

import aioboto3
from aiobotocore.config import AioConfig
from botocore.exceptions import ClientError
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from va_common.crypto.blob import DATA_KEY_SIZE, NONCE_SIZE, TAG_SIZE, CryptoError, DecryptionError
from va_common.settings import CryptoSettings, KmsProviderName

__all__ = [
    "LOCAL_KEK_ID",
    "NIL_ORG_ID",
    "AwsKmsProvider",
    "DataKey",
    "KmsClient",
    "KmsProvider",
    "LocalKmsProvider",
    "WrappedDataKey",
    "aws_kms_provider",
    "open_kms_provider",
]

NIL_ORG_ID: Final = UUID(int=0)
LOCAL_KEK_ID: Final = "local:v1"
_MASTER_KEY_SIZE: Final = 32
_KMS_DECRYPT_ERRORS: Final = frozenset({"InvalidCiphertextException", "IncorrectKeyException"})
_AWS_CLIENT_CONFIG: Final = AioConfig(
    connect_timeout=5,
    read_timeout=10,
    retries={"max_attempts": 3, "mode": "standard"},
)


@dataclass(frozen=True, slots=True)
class WrappedDataKey:
    """A data key as stored: the KEK that wrapped it and the wrapped bytes."""

    kek_id: str
    encrypted_data_key: bytes


@dataclass(frozen=True, slots=True)
class DataKey:
    """A freshly generated data key in plaintext and wrapped form; the plaintext never appears in repr."""

    kek_id: str
    plaintext: bytes = field(repr=False)
    encrypted_data_key: bytes

    @property
    def wrapped(self) -> WrappedDataKey:
        return WrappedDataKey(kek_id=self.kek_id, encrypted_data_key=self.encrypted_data_key)


class KmsProvider(Protocol):
    """Generates org-bound data keys and unwraps them."""

    async def generate_data_key(self, org_id: UUID) -> DataKey:
        """Create a new 256-bit data key wrapped under the KEK and bound to ``org_id``."""
        ...

    async def decrypt_data_key(self, org_id: UUID, wrapped: WrappedDataKey) -> bytes:
        """Unwrap a data key; raise :class:`DecryptionError` if it is not valid for ``org_id``."""
        ...


def _org_context(org_id: UUID) -> bytes:
    return f"org_id={org_id}".encode()


class LocalKmsProvider:
    """AES-256-GCM key wrapping under a 32-byte master key; wrapped key = ``nonce(12) | ciphertext | tag``."""

    def __init__(self, master_key: bytes, *, kek_id: str = LOCAL_KEK_ID) -> None:
        if len(master_key) != _MASTER_KEY_SIZE:
            msg = "local master key must be 32 bytes"
            raise CryptoError(msg)
        self._aead = AESGCM(master_key)
        self._kek_id = kek_id

    @classmethod
    def from_hex(cls, value: str) -> LocalKmsProvider:
        try:
            key = bytes.fromhex(value)
        except ValueError:
            msg = "LOCAL_MASTER_KEY must be 64 hex characters"
            raise CryptoError(msg) from None
        return cls(key)

    async def generate_data_key(self, org_id: UUID) -> DataKey:
        plaintext = os.urandom(DATA_KEY_SIZE)
        nonce = os.urandom(NONCE_SIZE)
        wrapped = nonce + self._aead.encrypt(nonce, plaintext, _org_context(org_id))
        return DataKey(kek_id=self._kek_id, plaintext=plaintext, encrypted_data_key=wrapped)

    async def decrypt_data_key(self, org_id: UUID, wrapped: WrappedDataKey) -> bytes:
        if wrapped.kek_id != self._kek_id:
            msg = "data key was wrapped by a different key-encryption key"
            raise DecryptionError(msg)
        blob = wrapped.encrypted_data_key
        if len(blob) < NONCE_SIZE + TAG_SIZE:
            msg = "wrapped data key is malformed"
            raise DecryptionError(msg)
        try:
            plaintext = self._aead.decrypt(blob[:NONCE_SIZE], blob[NONCE_SIZE:], _org_context(org_id))
        except InvalidTag:
            msg = "wrapped data key failed authentication"
            raise DecryptionError(msg) from None
        if len(plaintext) != DATA_KEY_SIZE:
            msg = "wrapped data key has the wrong length"
            raise DecryptionError(msg)
        return plaintext


class KmsClient(Protocol):
    """The subset of the aiobotocore KMS client used here, with boto3 keyword signatures."""

    async def generate_data_key(
        self,
        *,
        KeyId: str,  # noqa: N803
        KeySpec: Literal["AES_256"],  # noqa: N803
        EncryptionContext: Mapping[str, str],  # noqa: N803
    ) -> Mapping[str, Any]:
        """``kms:GenerateDataKey``."""
        ...

    async def decrypt(
        self,
        *,
        CiphertextBlob: bytes,  # noqa: N803
        KeyId: str,  # noqa: N803
        EncryptionContext: Mapping[str, str],  # noqa: N803
    ) -> Mapping[str, Any]:
        """``kms:Decrypt``."""
        ...


def _field_bytes(response: Mapping[str, Any], name: str) -> bytes:
    value = response.get(name)
    if not isinstance(value, bytes):
        msg = f"KMS response field {name} is missing or not bytes"
        raise CryptoError(msg)
    return value


def _field_str(response: Mapping[str, Any], name: str) -> str:
    value = response.get(name)
    if not isinstance(value, str) or not value:
        msg = f"KMS response field {name} is missing or not a string"
        raise CryptoError(msg)
    return value


class AwsKmsProvider:
    """AWS KMS data keys; ``kek_id`` is the key ARN that KMS reports."""

    def __init__(self, client: KmsClient, key_id: str) -> None:
        if not key_id.strip():
            msg = "AWS_KMS_KEY_ID is required for the aws KMS provider"
            raise CryptoError(msg)
        self._client = client
        self._key_id = key_id

    async def generate_data_key(self, org_id: UUID) -> DataKey:
        response = await self._client.generate_data_key(
            KeyId=self._key_id, KeySpec="AES_256", EncryptionContext={"org_id": str(org_id)}
        )
        plaintext = _field_bytes(response, "Plaintext")
        if len(plaintext) != DATA_KEY_SIZE:
            msg = "KMS returned a data key with the wrong length"
            raise CryptoError(msg)
        return DataKey(
            kek_id=_field_str(response, "KeyId"),
            plaintext=plaintext,
            encrypted_data_key=_field_bytes(response, "CiphertextBlob"),
        )

    async def decrypt_data_key(self, org_id: UUID, wrapped: WrappedDataKey) -> bytes:
        try:
            response = await self._client.decrypt(
                CiphertextBlob=wrapped.encrypted_data_key,
                KeyId=wrapped.kek_id,
                EncryptionContext={"org_id": str(org_id)},
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _KMS_DECRYPT_ERRORS:
                msg = "KMS rejected the wrapped data key"
                raise DecryptionError(msg) from None
            raise
        plaintext = _field_bytes(response, "Plaintext")
        if len(plaintext) != DATA_KEY_SIZE:
            msg = "KMS returned a data key with the wrong length"
            raise DecryptionError(msg)
        return plaintext


@asynccontextmanager
async def aws_kms_provider(
    key_id: str, *, region_name: str | None = None, session: aioboto3.Session | None = None
) -> AsyncIterator[AwsKmsProvider]:
    """Open an aiobotocore KMS client with bounded timeouts and retries and yield a provider over it."""
    kms_session = session if session is not None else aioboto3.Session()
    async with kms_session.client("kms", region_name=region_name, config=_AWS_CLIENT_CONFIG) as client:
        yield AwsKmsProvider(client, key_id)


@asynccontextmanager
async def open_kms_provider(settings: CryptoSettings) -> AsyncIterator[KmsProvider]:
    """Yield the provider selected by ``KMS_PROVIDER``."""
    if settings.kms_provider is KmsProviderName.AWS:
        async with aws_kms_provider(settings.aws_kms_key_id) as provider:
            yield provider
        return
    yield LocalKmsProvider.from_hex(settings.local_master_key.get_secret_value())
