"""Envelope ciphertext blob format and crypto exceptions (specs/28-security.md §3).

Layout: ``version(1)=0x01 | kek_id_len(1) | kek_id(utf8) | edk_len(2, big-endian) | encrypted_data_key |
nonce(12) | ciphertext | tag(16)``. ``EncryptedBlob.ciphertext`` holds the AES-GCM output, which ends with
the tag.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

__all__ = [
    "BLOB_VERSION",
    "DATA_KEY_SIZE",
    "NONCE_SIZE",
    "TAG_SIZE",
    "CryptoError",
    "DecryptionError",
    "EncryptedBlob",
    "column_aad",
]

BLOB_VERSION: Final = 0x01
NONCE_SIZE: Final = 12
TAG_SIZE: Final = 16
DATA_KEY_SIZE: Final = 32
_MAX_KEK_ID_LEN: Final = 0xFF
_MAX_EDK_LEN: Final = 0xFFFF


class CryptoError(Exception):
    """Encryption misconfiguration or a missing key."""


class DecryptionError(CryptoError):
    """Malformed, tampered, or mis-bound ciphertext. Messages never carry key or plaintext material."""


def _take(data: bytes, offset: int, size: int) -> tuple[bytes, int]:
    end = offset + size
    if size <= 0 or end > len(data):
        msg = "malformed encrypted blob"
        raise DecryptionError(msg)
    return data[offset:end], end


@dataclass(frozen=True, slots=True)
class EncryptedBlob:
    """Parsed envelope ciphertext: the wrapped data key that sealed it, the nonce, and ciphertext + tag."""

    kek_id: str
    encrypted_data_key: bytes
    nonce: bytes
    ciphertext: bytes

    def to_bytes(self) -> bytes:
        kek_id = self.kek_id.encode()
        if not 1 <= len(kek_id) <= _MAX_KEK_ID_LEN:
            msg = f"kek_id must be 1..{_MAX_KEK_ID_LEN} UTF-8 bytes"
            raise CryptoError(msg)
        if not 1 <= len(self.encrypted_data_key) <= _MAX_EDK_LEN:
            msg = f"encrypted data key must be 1..{_MAX_EDK_LEN} bytes"
            raise CryptoError(msg)
        if len(self.nonce) != NONCE_SIZE or len(self.ciphertext) < TAG_SIZE:
            msg = "nonce must be 12 bytes and ciphertext must include the 16-byte tag"
            raise CryptoError(msg)
        return b"".join(
            (
                bytes((BLOB_VERSION, len(kek_id))),
                kek_id,
                len(self.encrypted_data_key).to_bytes(2, "big"),
                self.encrypted_data_key,
                self.nonce,
                self.ciphertext,
            )
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> EncryptedBlob:
        version, offset = _take(data, 0, 1)
        if version[0] != BLOB_VERSION:
            msg = "unsupported encrypted blob version"
            raise DecryptionError(msg)
        kek_len, offset = _take(data, offset, 1)
        kek_raw, offset = _take(data, offset, kek_len[0])
        edk_len, offset = _take(data, offset, 2)
        edk, offset = _take(data, offset, int.from_bytes(edk_len, "big"))
        nonce, offset = _take(data, offset, NONCE_SIZE)
        if len(data) - offset < TAG_SIZE:
            msg = "malformed encrypted blob"
            raise DecryptionError(msg)
        try:
            kek_id = kek_raw.decode()
        except UnicodeDecodeError:
            msg = "malformed encrypted blob"
            raise DecryptionError(msg) from None
        return cls(kek_id=kek_id, encrypted_data_key=edk, nonce=nonce, ciphertext=data[offset:])


def column_aad(table: str, column: str, row_id: UUID | str) -> str:
    """Associated data binding a ciphertext to its storage location: ``"{table}.{column}:{row_id}"``."""
    return f"{table}.{column}:{row_id}"
