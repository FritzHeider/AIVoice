"""SigningKey: generated and configured keys, signatures verified independently with pynacl."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey as NaclSigningKey
from nacl.signing import VerifyKey

from va_sim.telnyx.keys import SigningKey

SEED = bytes(range(32))


def _verify(public_key_b64: str, message: bytes, signature_b64: str) -> None:
    VerifyKey(base64.b64decode(public_key_b64)).verify(message, base64.b64decode(signature_b64))


def test_generated_key_signs_verifiably() -> None:
    key = SigningKey.generate()
    _verify(key.public_key_b64, b"hello", key.sign(b"hello"))
    with pytest.raises(BadSignatureError):
        _verify(key.public_key_b64, b"hellO", key.sign(b"hello"))
    assert SigningKey.generate().public_key_b64 != key.public_key_b64


def test_signature_headers_cover_timestamp_and_body() -> None:
    key = SigningKey.generate()
    headers = key.signature_headers(1_767_225_600, b'{"a":1}')
    assert headers["telnyx-timestamp"] == "1767225600"
    _verify(key.public_key_b64, b'1767225600|{"a":1}', headers["telnyx-signature-ed25519"])


def test_seed_forms_give_the_pynacl_public_key() -> None:
    expected = base64.b64encode(bytes(NaclSigningKey(SEED).verify_key)).decode()
    from_seed = SigningKey.from_base64(base64.b64encode(SEED).decode())
    full = SEED + base64.b64decode(expected)
    from_full = SigningKey.from_base64(f"  {base64.b64encode(full).decode()}\n")
    assert from_seed.public_key_b64 == from_full.public_key_b64 == expected


@pytest.mark.parametrize(
    ("secret", "message"),
    [
        ("not base64!", "not valid base64"),
        (base64.b64encode(b"short").decode(), "32 or 64 bytes"),
        (base64.b64encode(SEED + bytes(32)).decode(), "does not match"),
    ],
)
def test_bad_secrets_are_rejected(secret: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        SigningKey.from_base64(secret)


def test_write_public_key_creates_parent_directories(tmp_path: Path) -> None:
    key = SigningKey.generate()
    target = tmp_path / ".sim" / "nested" / "telnyx_public_key"
    key.write_public_key(target)
    assert target.read_text(encoding="utf-8") == f"{key.public_key_b64}\n"
