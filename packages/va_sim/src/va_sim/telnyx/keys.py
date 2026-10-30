"""FakeTelnyx Ed25519 signing key (specs/30-testing-and-evals.md §3, §3.2; specs/06-telephony-telnyx.md §4.1).

Webhooks carry ``telnyx-signature-ed25519`` (base64 signature over ``"{timestamp}|{raw body}"``) and
``telnyx-timestamp`` (unix seconds). The key comes from ``SIM_ED25519_PRIVATE_KEY`` (base64 of the 32-byte
seed, or the 64-byte seed followed by its public key) or is generated when the simulator state is created.
"""

from __future__ import annotations

import base64
import binascii
from pathlib import Path
from typing import Final, Self

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

__all__ = ["PUBLIC_KEY_FILE", "SIGNATURE_HEADER", "TIMESTAMP_HEADER", "SigningKey"]

PUBLIC_KEY_FILE: Final = Path(".sim/telnyx_public_key")
SIGNATURE_HEADER: Final = "telnyx-signature-ed25519"
TIMESTAMP_HEADER: Final = "telnyx-timestamp"
_SEED_BYTES: Final = 32


class SigningKey:
    """An Ed25519 private key that signs FakeTelnyx webhooks."""

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._private_key = private_key
        raw = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self._public_key_b64 = base64.b64encode(raw).decode("ascii")

    @classmethod
    def generate(cls) -> Self:
        """A fresh random keypair."""
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_base64(cls, secret: str) -> Self:
        """The key encoded in ``secret``: base64 of the seed, or of the seed followed by its public key."""
        try:
            raw = base64.b64decode(secret.strip(), validate=True)
        except binascii.Error as exc:
            msg = "the Ed25519 private key is not valid base64"
            raise ValueError(msg) from exc
        if len(raw) not in {_SEED_BYTES, 2 * _SEED_BYTES}:
            msg = f"the Ed25519 private key must decode to 32 or 64 bytes, got {len(raw)}"
            raise ValueError(msg)
        key = cls(Ed25519PrivateKey.from_private_bytes(raw[:_SEED_BYTES]))
        if len(raw) == 2 * _SEED_BYTES and base64.b64decode(key.public_key_b64) != raw[_SEED_BYTES:]:
            msg = "the public half of the 64-byte Ed25519 key does not match its seed"
            raise ValueError(msg)
        return key

    @property
    def public_key_b64(self) -> str:
        """The raw 32-byte public key, base64 (the ``TELNYX_PUBLIC_KEY`` format)."""
        return self._public_key_b64

    def sign(self, message: bytes) -> str:
        """The base64 Ed25519 signature of ``message``."""
        return base64.b64encode(self._private_key.sign(message)).decode("ascii")

    def signature_headers(self, timestamp: int, body: bytes) -> dict[str, str]:
        """Telnyx webhook signature headers for ``body`` sent at unix time ``timestamp``."""
        return {
            SIGNATURE_HEADER: self.sign(f"{timestamp}|".encode() + body),
            TIMESTAMP_HEADER: str(timestamp),
        }

    def write_public_key(self, path: Path) -> None:
        """Write the base64 public key (plus a newline) to ``path``, creating parent directories."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{self._public_key_b64}\n", encoding="utf-8")
