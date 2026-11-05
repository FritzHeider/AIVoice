"""Secret header encryption helpers (specs/13-tools-and-dynamic-data.md §3).

Rules: specs/13 §2 Decision P08-T01. ``tools.secret_headers_enc`` holds the envelope-encrypted JSON object of
the tool's secret headers, bound to the AAD ``tool:{tool_id}``, so a blob copied onto another tool row fails
to decrypt. Values are decrypted only to run the tool or to build the masked view: API responses, audit rows,
and ``call.tool`` events never carry a plaintext value.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Final, Protocol

from va_common.crypto import DecryptionError
from va_common.json import dumps, loads

__all__ = [
    "MASK",
    "MASK_REVEAL_MIN_LENGTH",
    "SecretCipher",
    "decrypt_secret_headers",
    "encrypt_secret_headers",
    "mask_headers",
    "mask_secret",
    "secret_headers_aad",
]

MASK: Final = "••••"
MASK_REVEAL_MIN_LENGTH: Final = 12
"""Shorter values are masked completely: their last four characters would give most of them away."""
_REVEALED: Final = 4


class SecretCipher(Protocol):
    """The part of :class:`va_common.crypto.Envelope` the helpers use."""

    async def encrypt(self, org_id: uuid.UUID, plaintext: bytes, aad: str) -> bytes: ...

    async def decrypt(self, org_id: uuid.UUID, blob: bytes, aad: str) -> bytes: ...


def secret_headers_aad(tool_id: uuid.UUID) -> str:
    """The AAD binding a secret header blob to its tool row."""
    return f"tool:{tool_id}"


async def encrypt_secret_headers(
    cipher: SecretCipher, org_id: uuid.UUID, tool_id: uuid.UUID, headers: Mapping[str, str]
) -> bytes | None:
    """The blob for ``headers`` (canonical JSON, sorted keys); ``None`` when there are none."""
    if not headers:
        return None
    return await cipher.encrypt(org_id, dumps(dict(headers), sort_keys=True), secret_headers_aad(tool_id))


async def decrypt_secret_headers(
    cipher: SecretCipher, org_id: uuid.UUID, tool_id: uuid.UUID, blob: bytes | None
) -> dict[str, str]:
    """The secret headers in ``blob`` (``{}`` for none); :class:`DecryptionError` for a foreign blob."""
    if blob is None:
        return {}
    value = loads(await cipher.decrypt(org_id, blob, secret_headers_aad(tool_id)))
    if not isinstance(value, dict) or not all(
        isinstance(name, str) and isinstance(item, str) for name, item in value.items()
    ):
        msg = "secret headers blob is not a JSON object of strings"
        raise DecryptionError(msg)
    return {str(name): str(item) for name, item in value.items()}


def mask_secret(value: str) -> str:
    """``"••••" + last 4`` for values of at least 12 characters, else ``"••••"``."""
    if len(value) >= MASK_REVEAL_MIN_LENGTH:
        return MASK + value[-_REVEALED:]
    return MASK


def mask_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Every header value masked with :func:`mask_secret`."""
    return {name: mask_secret(value) for name, value in headers.items()}
