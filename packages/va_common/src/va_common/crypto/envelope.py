"""Envelope encryption of tenant secrets with per-org data keys (specs/28-security.md §3)."""

from __future__ import annotations

import os
from typing import Protocol
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from va_common.crypto.blob import NONCE_SIZE, CryptoError, DecryptionError, EncryptedBlob
from va_common.crypto.cache import DataKeyCache
from va_common.crypto.kms import NIL_ORG_ID, KmsProvider, WrappedDataKey

__all__ = ["DataKeyStore", "Envelope"]


class DataKeyStore(Protocol):
    """Source of each org's active wrapped data key (backed by ``org_data_keys`` with ``status='active'``)."""

    async def active_data_key(self, org_id: UUID) -> WrappedDataKey | None:
        """The org's active wrapped data key, or ``None`` when it has none."""
        ...


class Envelope:
    """AES-256-GCM encryption under per-org data keys; every blob embeds the wrapped key that sealed it."""

    def __init__(
        self, provider: KmsProvider, store: DataKeyStore, *, cache: DataKeyCache | None = None
    ) -> None:
        self._provider = provider
        self._store = store
        self._cache = cache if cache is not None else DataKeyCache()

    async def create_data_key(self, org_id: UUID) -> WrappedDataKey:
        """Generate a new data key for ``org_id``; the caller persists the returned wrapped key."""
        data_key = await self._provider.generate_data_key(org_id)
        self._cache.put(org_id, data_key.wrapped, data_key.plaintext)
        return data_key.wrapped

    async def _active_key(self, org_id: UUID) -> WrappedDataKey:
        wrapped = await self._store.active_data_key(org_id)
        if wrapped is None:
            msg = f"no active data key for org {org_id}"
            raise CryptoError(msg)
        return wrapped

    async def _unwrap(self, org_id: UUID, wrapped: WrappedDataKey) -> bytes:
        plaintext = self._cache.get(org_id, wrapped)
        if plaintext is None:
            plaintext = await self._provider.decrypt_data_key(org_id, wrapped)
            self._cache.put(org_id, wrapped, plaintext)
        return plaintext

    async def _seal(self, org_id: UUID, wrapped: WrappedDataKey, plaintext: bytes, aad: str) -> bytes:
        data_key = await self._unwrap(org_id, wrapped)
        nonce = os.urandom(NONCE_SIZE)
        ciphertext = AESGCM(data_key).encrypt(nonce, plaintext, aad.encode())
        return EncryptedBlob(
            kek_id=wrapped.kek_id,
            encrypted_data_key=wrapped.encrypted_data_key,
            nonce=nonce,
            ciphertext=ciphertext,
        ).to_bytes()

    async def _open(self, org_id: UUID, parsed: EncryptedBlob, aad: str) -> bytes:
        wrapped = WrappedDataKey(kek_id=parsed.kek_id, encrypted_data_key=parsed.encrypted_data_key)
        data_key = await self._unwrap(org_id, wrapped)
        try:
            return AESGCM(data_key).decrypt(parsed.nonce, parsed.ciphertext, aad.encode())
        except InvalidTag:
            msg = "ciphertext failed authentication"
            raise DecryptionError(msg) from None

    async def encrypt(self, org_id: UUID, plaintext: bytes, aad: str) -> bytes:
        """Encrypt under the org's active data key, bound to ``aad`` (use :func:`column_aad`)."""
        return await self._seal(org_id, await self._active_key(org_id), plaintext, aad)

    async def decrypt(self, org_id: UUID, blob: bytes, aad: str) -> bytes:
        """Decrypt with the data key embedded in ``blob``; raise :class:`DecryptionError` on any mismatch."""
        return await self._open(org_id, EncryptedBlob.from_bytes(blob), aad)

    async def encrypt_str(self, org_id: UUID, plaintext: str, aad: str) -> bytes:
        return await self.encrypt(org_id, plaintext.encode(), aad)

    async def decrypt_str(self, org_id: UUID, blob: bytes, aad: str) -> str:
        plaintext = await self.decrypt(org_id, blob, aad)
        try:
            return plaintext.decode()
        except UnicodeDecodeError:
            msg = "decrypted value is not valid UTF-8"
            raise DecryptionError(msg) from None

    async def platform_encrypt(self, plaintext: bytes, aad: str) -> bytes:
        """Encrypt a platform-level secret (org id = nil UUID)."""
        return await self.encrypt(NIL_ORG_ID, plaintext, aad)

    async def platform_decrypt(self, blob: bytes, aad: str) -> bytes:
        return await self.decrypt(NIL_ORG_ID, blob, aad)

    async def rewrap(self, org_id: UUID, blob: bytes, aad: str) -> bytes:
        """Re-encrypt ``blob`` under the org's active data key; return it unchanged if already current."""
        parsed = EncryptedBlob.from_bytes(blob)
        plaintext = await self._open(org_id, parsed, aad)
        active = await self._active_key(org_id)
        if (parsed.kek_id, parsed.encrypted_data_key) == (active.kek_id, active.encrypted_data_key):
            return blob
        return await self._seal(org_id, active, plaintext, aad)
