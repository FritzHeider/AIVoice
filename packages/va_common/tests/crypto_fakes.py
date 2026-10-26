"""Test doubles for `va_common.crypto`: an in-memory data-key store and a fake aiobotocore KMS client."""

from __future__ import annotations

import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from botocore.exceptions import ClientError
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from va_common.crypto import DataKey, KmsProvider, WrappedDataKey

_NONCE = 12


class InMemoryDataKeyStore:
    """`DataKeyStore` over a dict of org id -> active wrapped data key."""

    def __init__(self) -> None:
        self.active: dict[UUID, WrappedDataKey] = {}

    def activate(self, org_id: UUID, wrapped: WrappedDataKey) -> None:
        self.active[org_id] = wrapped

    async def active_data_key(self, org_id: UUID) -> WrappedDataKey | None:
        return self.active.get(org_id)


def _context_aad(context: Mapping[str, str]) -> bytes:
    return "&".join(f"{k}={v}" for k, v in sorted(context.items())).encode()


def _client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"fake {code}"}}, operation)


@dataclass
class FakeKmsClient:
    """In-memory KMS: AES-GCM wraps data keys under a random master key, bound to the encryption context."""

    key_arn: str = field(default_factory=lambda: f"arn:aws:kms:us-east-1:111122223333:key/{uuid.uuid4()}")
    master_key: bytes = field(default_factory=lambda: AESGCM.generate_key(bit_length=256))
    data_key_size: int = 32
    decrypt_error: str | None = None
    decrypt_plaintext: bytes | None = None
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def generate_data_key(
        self,
        *,
        KeyId: str,  # noqa: N803
        KeySpec: Literal["AES_256"],  # noqa: N803
        EncryptionContext: Mapping[str, str],  # noqa: N803
    ) -> Mapping[str, Any]:
        self.calls.append(
            (
                "GenerateDataKey",
                {"KeyId": KeyId, "KeySpec": KeySpec, "EncryptionContext": dict(EncryptionContext)},
            )
        )
        plaintext = os.urandom(self.data_key_size)
        nonce = os.urandom(_NONCE)
        blob = nonce + AESGCM(self.master_key).encrypt(nonce, plaintext, _context_aad(EncryptionContext))
        return {"KeyId": self.key_arn, "Plaintext": plaintext, "CiphertextBlob": blob}

    async def decrypt(
        self,
        *,
        CiphertextBlob: bytes,  # noqa: N803
        KeyId: str,  # noqa: N803
        EncryptionContext: Mapping[str, str],  # noqa: N803
    ) -> Mapping[str, Any]:
        self.calls.append(("Decrypt", {"KeyId": KeyId, "EncryptionContext": dict(EncryptionContext)}))
        if self.decrypt_error is not None:
            raise _client_error(self.decrypt_error, "Decrypt")
        if KeyId != self.key_arn:
            raise _client_error("IncorrectKeyException", "Decrypt")
        try:
            plaintext = AESGCM(self.master_key).decrypt(
                CiphertextBlob[:_NONCE], CiphertextBlob[_NONCE:], _context_aad(EncryptionContext)
            )
        except (InvalidTag, ValueError):
            raise _client_error("InvalidCiphertextException", "Decrypt") from None
        if self.decrypt_plaintext is not None:
            plaintext = self.decrypt_plaintext
        return {"KeyId": self.key_arn, "Plaintext": plaintext}


class CountingProvider:
    """Wraps a `KmsProvider` and counts calls, to observe the data-key cache."""

    def __init__(self, inner: KmsProvider) -> None:
        self.inner = inner
        self.generated = 0
        self.decrypted = 0

    async def generate_data_key(self, org_id: UUID) -> DataKey:
        self.generated += 1
        return await self.inner.generate_data_key(org_id)

    async def decrypt_data_key(self, org_id: UUID, wrapped: WrappedDataKey) -> bytes:
        self.decrypted += 1
        return await self.inner.decrypt_data_key(org_id, wrapped)
