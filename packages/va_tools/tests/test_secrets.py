"""Secret header encryption and masking (specs/13 §2-§3, Decision P08-T01)."""

from __future__ import annotations

import secrets
import uuid

import pytest
from crypto_fakes import InMemoryDataKeyStore

from va_common.crypto import DecryptionError, Envelope, LocalKmsProvider
from va_common.ids import new_id
from va_tools.secrets import (
    MASK,
    decrypt_secret_headers,
    encrypt_secret_headers,
    mask_headers,
    mask_secret,
    secret_headers_aad,
)


async def _envelope(org_id: uuid.UUID) -> Envelope:
    store = InMemoryDataKeyStore()
    envelope = Envelope(LocalKmsProvider.from_hex(secrets.token_hex(32)), store)
    store.activate(org_id, await envelope.create_data_key(org_id))
    return envelope


async def test_round_trip_returns_the_headers() -> None:
    org_id, tool_id = new_id(), new_id()
    envelope = await _envelope(org_id)
    headers = {"Authorization": f"Bearer {secrets.token_hex(16)}", "X-Api-Key": secrets.token_hex(8)}
    blob = await encrypt_secret_headers(envelope, org_id, tool_id, headers)
    assert blob is not None
    assert headers["Authorization"].encode() not in blob
    assert await decrypt_secret_headers(envelope, org_id, tool_id, blob) == headers


async def test_no_headers_store_nothing() -> None:
    org_id, tool_id = new_id(), new_id()
    envelope = await _envelope(org_id)
    assert await encrypt_secret_headers(envelope, org_id, tool_id, {}) is None
    assert await decrypt_secret_headers(envelope, org_id, tool_id, None) == {}


async def test_a_blob_is_bound_to_its_tool() -> None:
    org_id, tool_id = new_id(), new_id()
    envelope = await _envelope(org_id)
    blob = await encrypt_secret_headers(envelope, org_id, tool_id, {"Authorization": secrets.token_hex(8)})
    with pytest.raises(DecryptionError):
        await decrypt_secret_headers(envelope, org_id, new_id(), blob)


async def test_a_blob_that_is_not_an_object_of_strings_is_refused() -> None:
    org_id, tool_id = new_id(), new_id()
    envelope = await _envelope(org_id)
    blob = await envelope.encrypt(org_id, b'{"Authorization": 1}', secret_headers_aad(tool_id))
    with pytest.raises(DecryptionError, match="object of strings"):
        await decrypt_secret_headers(envelope, org_id, tool_id, blob)


def test_aad_names_the_tool() -> None:
    tool_id = new_id()
    assert secret_headers_aad(tool_id) == f"tool:{tool_id}"


@pytest.mark.parametrize(
    ("length", "revealed"),
    [(1, False), (11, False), (12, True), (40, True)],
)
def test_mask_reveals_the_last_four_only_for_long_values(length: int, revealed: bool) -> None:
    value = secrets.token_hex(length)[:length]
    masked = mask_secret(value)
    assert masked == (MASK + value[-4:] if revealed else MASK)


def test_mask_headers_masks_every_value() -> None:
    value = secrets.token_hex(10)
    assert mask_headers({"Authorization": value, "X-Short": "abc"}) == {
        "Authorization": MASK + value[-4:],
        "X-Short": MASK,
    }
