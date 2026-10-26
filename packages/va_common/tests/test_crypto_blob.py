from __future__ import annotations

import os
import uuid

import pytest
from hypothesis import given
from hypothesis import strategies as st

from va_common.crypto import (
    BLOB_VERSION,
    NONCE_SIZE,
    TAG_SIZE,
    CryptoError,
    DecryptionError,
    EncryptedBlob,
    column_aad,
)


def make_blob(
    kek_id: str = "local:v1", edk: bytes = b"\x01" * 60, ciphertext: bytes = b"\x02" * 20
) -> EncryptedBlob:
    return EncryptedBlob(
        kek_id=kek_id, encrypted_data_key=edk, nonce=b"\x03" * NONCE_SIZE, ciphertext=ciphertext
    )


def test_byte_layout_matches_spec() -> None:
    edk = os.urandom(300)
    nonce = os.urandom(NONCE_SIZE)
    ciphertext = os.urandom(TAG_SIZE + 5)
    data = EncryptedBlob("local:v1", edk, nonce, ciphertext).to_bytes()
    assert data[0] == BLOB_VERSION == 0x01
    assert data[1] == len("local:v1")
    assert data[2:10] == b"local:v1"
    assert data[10:12] == (300).to_bytes(2, "big") == b"\x01\x2c"
    assert data[12:312] == edk
    assert data[312:324] == nonce
    assert data[324:] == ciphertext
    assert EncryptedBlob.from_bytes(data) == EncryptedBlob("local:v1", edk, nonce, ciphertext)


@given(
    kek_id=st.text(min_size=1, max_size=63),
    edk=st.binary(min_size=1, max_size=600),
    ciphertext=st.binary(min_size=TAG_SIZE, max_size=200),
)
def test_round_trip(kek_id: str, edk: bytes, ciphertext: bytes) -> None:
    blob = make_blob(kek_id, edk, ciphertext)
    assert EncryptedBlob.from_bytes(blob.to_bytes()) == blob


def test_max_lengths_round_trip() -> None:
    blob = make_blob("k" * 255, b"\x05" * 0xFFFF)
    assert EncryptedBlob.from_bytes(blob.to_bytes()) == blob


def _valid() -> bytes:
    return make_blob().to_bytes()


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"\x02" + _valid()[1:], id="bad-version"),
        pytest.param(b"\x01", id="no-kek-len"),
        pytest.param(b"\x01\x00" + _valid()[2:], id="zero-kek-len"),
        pytest.param(b"\x01\x08local", id="truncated-kek-id"),
        pytest.param(
            b"\x01\x02\xff\xfe\x00\x01\x01" + b"\x03" * NONCE_SIZE + b"\x00" * TAG_SIZE, id="non-utf8-kek"
        ),
        pytest.param(_valid()[:10], id="no-edk-len"),
        pytest.param(_valid()[:11], id="half-edk-len"),
        pytest.param(b"\x01\x01k\x00\x00" + b"\x03" * NONCE_SIZE + b"\x00" * TAG_SIZE, id="zero-edk-len"),
        pytest.param(_valid()[:40], id="truncated-edk"),
        pytest.param(_valid()[: 12 + 60 + 5], id="truncated-nonce"),
        pytest.param(_valid()[: 12 + 60 + NONCE_SIZE + TAG_SIZE - 1], id="short-ciphertext"),
    ],
)
def test_malformed_blobs_raise_decryption_error(data: bytes) -> None:
    with pytest.raises(DecryptionError):
        EncryptedBlob.from_bytes(data)


@pytest.mark.parametrize(
    "blob",
    [
        pytest.param(make_blob(kek_id="k" * 256), id="kek-too-long"),
        pytest.param(make_blob(kek_id="é" * 128), id="kek-too-long-utf8"),
        pytest.param(make_blob(kek_id=""), id="kek-empty"),
        pytest.param(make_blob(edk=b"\x01" * 0x10000), id="edk-too-long"),
        pytest.param(make_blob(edk=b""), id="edk-empty"),
        pytest.param(make_blob(ciphertext=b"\x00" * (TAG_SIZE - 1)), id="ciphertext-without-tag"),
        pytest.param(EncryptedBlob("k", b"\x01", b"\x00" * 8, b"\x00" * TAG_SIZE), id="short-nonce"),
    ],
)
def test_invalid_blobs_refuse_to_encode(blob: EncryptedBlob) -> None:
    with pytest.raises(CryptoError) as info:
        blob.to_bytes()
    assert not isinstance(info.value, DecryptionError)


def test_column_aad_format() -> None:
    row = uuid.UUID("01890000-0000-7000-8000-000000000001")
    assert column_aad("tools", "secret_headers_enc", row) == f"tools.secret_headers_enc:{row}"
    assert column_aad("orgs", "webhook_signing_secret_enc", "abc") == "orgs.webhook_signing_secret_enc:abc"
