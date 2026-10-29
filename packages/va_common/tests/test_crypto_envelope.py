from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import pytest
from crypto_fakes import CountingProvider, FakeKmsClient, InMemoryDataKeyStore
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from va_common.crypto import (
    NIL_ORG_ID,
    AwsKmsProvider,
    CryptoError,
    DataKeyCache,
    DecryptionError,
    EncryptedBlob,
    Envelope,
    KmsProvider,
    LocalKmsProvider,
    column_aad,
)

ROW_A = uuid.UUID("01890000-0000-7000-8000-00000000000a")
ROW_B = uuid.UUID("01890000-0000-7000-8000-00000000000b")
AAD = column_aad("tools", "secret_headers_enc", ROW_A)


@dataclass
class Harness:
    envelope: Envelope
    store: InMemoryDataKeyStore
    provider: CountingProvider
    org: uuid.UUID

    async def rotate(self, org: uuid.UUID | None = None) -> None:
        target = org if org is not None else self.org
        self.store.activate(target, await self.envelope.create_data_key(target))


PROVIDERS: dict[str, Callable[[], KmsProvider]] = {
    "local": lambda: LocalKmsProvider(os.urandom(32)),
    "aws": lambda: AwsKmsProvider(FakeKmsClient(), "alias/voiceagents"),
}


@pytest.fixture(params=sorted(PROVIDERS))
async def harness(request: pytest.FixtureRequest) -> AsyncIterator[Harness]:
    provider = CountingProvider(PROVIDERS[request.param]())
    store = InMemoryDataKeyStore()
    h = Harness(Envelope(provider, store), store, provider, uuid.uuid4())
    await h.rotate()
    yield h


def fresh_envelope(h: Harness) -> Envelope:
    """Same provider and store, empty cache: forces data keys to be unwrapped through the provider."""
    return Envelope(h.provider, h.store, cache=DataKeyCache())


@pytest.mark.parametrize("size", [0, 1, 19, 4096])
async def test_round_trip(harness: Harness, size: int) -> None:
    plaintext = os.urandom(size)
    blob = await harness.envelope.encrypt(harness.org, plaintext, AAD)
    assert plaintext == await harness.envelope.decrypt(harness.org, blob, AAD)
    assert plaintext == await fresh_envelope(harness).decrypt(harness.org, blob, AAD)


@settings(max_examples=25, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(plaintext=st.binary(max_size=512))
async def test_round_trip_property(harness: Harness, plaintext: bytes) -> None:
    blob = await harness.envelope.encrypt(harness.org, plaintext, AAD)
    assert await harness.envelope.decrypt(harness.org, blob, AAD) == plaintext


async def test_blob_embeds_active_key_and_random_nonce(harness: Harness) -> None:
    active = harness.store.active[harness.org]
    first = await harness.envelope.encrypt(harness.org, b"same", AAD)
    second = await harness.envelope.encrypt(harness.org, b"same", AAD)
    assert first != second
    parsed = EncryptedBlob.from_bytes(first)
    assert (parsed.kek_id, parsed.encrypted_data_key) == (active.kek_id, active.encrypted_data_key)
    assert len(parsed.ciphertext) == len(b"same") + 16
    assert b"same" not in first


async def test_row_swap_is_detected(harness: Harness) -> None:
    blob = await harness.envelope.encrypt(harness.org, b"v", AAD)
    with pytest.raises(DecryptionError):
        await harness.envelope.decrypt(harness.org, blob, column_aad("tools", "secret_headers_enc", ROW_B))


async def test_wrong_org_is_rejected(harness: Harness) -> None:
    other = uuid.uuid4()
    await harness.rotate(other)
    blob = await harness.envelope.encrypt(harness.org, b"v", AAD)
    with pytest.raises(DecryptionError):
        await harness.envelope.decrypt(other, blob, AAD)


def _flip(blob: bytes, index: int) -> bytes:
    data = bytearray(blob)
    data[index] ^= 0x80
    return bytes(data)


@pytest.mark.parametrize("region", ["edk", "nonce", "ciphertext", "tag"])
async def test_tampering_is_detected(harness: Harness, region: str) -> None:
    blob = await harness.envelope.encrypt(harness.org, b"payload-bytes", AAD)
    parsed = EncryptedBlob.from_bytes(blob)
    nonce_at = len(blob) - len(parsed.ciphertext) - 12
    index = {
        "edk": nonce_at - 1,
        "nonce": nonce_at,
        "ciphertext": nonce_at + 12,
        "tag": len(blob) - 1,
    }[region]
    with pytest.raises(DecryptionError):
        await fresh_envelope(harness).decrypt(harness.org, _flip(blob, index), AAD)


async def test_truncated_blob(harness: Harness) -> None:
    blob = await harness.envelope.encrypt(harness.org, b"v", AAD)
    with pytest.raises(DecryptionError):
        await harness.envelope.decrypt(harness.org, blob[:-17], AAD)


async def test_missing_active_key(harness: Harness) -> None:
    with pytest.raises(CryptoError, match="no active data key") as info:
        await harness.envelope.encrypt(uuid.uuid4(), b"v", AAD)
    assert not isinstance(info.value, DecryptionError)


async def test_encrypt_with_key_decrypts_with_store_less_envelope(harness: Harness) -> None:
    org = uuid.uuid4()
    wrapped = await harness.envelope.create_data_key(org)
    blob = await harness.envelope.encrypt_with_key(org, wrapped, b"uncommitted", AAD)
    parsed = EncryptedBlob.from_bytes(blob)
    assert (parsed.kek_id, parsed.encrypted_data_key) == (wrapped.kek_id, wrapped.encrypted_data_key)
    assert org not in harness.store.active
    envelope = fresh_envelope(harness)
    assert await envelope.decrypt(org, blob, AAD) == b"uncommitted"
    with pytest.raises(DecryptionError):
        await envelope.decrypt(org, blob, column_aad("tools", "secret_headers_enc", ROW_B))


async def test_old_blobs_decrypt_after_rotation(harness: Harness) -> None:
    old = await harness.envelope.encrypt(harness.org, b"before", AAD)
    await harness.rotate()
    new = await harness.envelope.encrypt(harness.org, b"after", AAD)
    assert (
        EncryptedBlob.from_bytes(old).encrypted_data_key != EncryptedBlob.from_bytes(new).encrypted_data_key
    )
    envelope = fresh_envelope(harness)
    assert await envelope.decrypt(harness.org, old, AAD) == b"before"
    assert await envelope.decrypt(harness.org, new, AAD) == b"after"


async def test_rewrap_moves_blob_to_active_key(harness: Harness) -> None:
    old = await harness.envelope.encrypt(harness.org, b"rotate me", AAD)
    assert await harness.envelope.rewrap(harness.org, old, AAD) is old
    await harness.rotate()
    rewrapped = await harness.envelope.rewrap(harness.org, old, AAD)
    active = harness.store.active[harness.org]
    assert EncryptedBlob.from_bytes(rewrapped).encrypted_data_key == active.encrypted_data_key
    assert await fresh_envelope(harness).decrypt(harness.org, rewrapped, AAD) == b"rotate me"
    assert await harness.envelope.rewrap(harness.org, rewrapped, AAD) is rewrapped


async def test_rewrap_refuses_wrong_aad(harness: Harness) -> None:
    blob = await harness.envelope.encrypt(harness.org, b"v", AAD)
    await harness.rotate()
    with pytest.raises(DecryptionError):
        await harness.envelope.rewrap(harness.org, blob, column_aad("tools", "secret_headers_enc", ROW_B))


async def test_string_helpers(harness: Harness) -> None:
    text = "Grüße, 東京 — ☎️"
    blob = await harness.envelope.encrypt_str(harness.org, text, AAD)
    assert await harness.envelope.decrypt_str(harness.org, blob, AAD) == text


async def test_decrypt_str_rejects_invalid_utf8(harness: Harness) -> None:
    blob = await harness.envelope.encrypt(harness.org, b"\xff\xfe\xfd", AAD)
    with pytest.raises(DecryptionError, match="UTF-8"):
        await harness.envelope.decrypt_str(harness.org, blob, AAD)


PLATFORM_AAD = column_aad("users", "mfa_totp_secret_enc", ROW_A)


async def test_platform_secrets_use_nil_org(harness: Harness) -> None:
    blob = await harness.envelope.platform_encrypt(b"platform", PLATFORM_AAD)
    assert await harness.envelope.platform_decrypt(blob, PLATFORM_AAD) == b"platform"
    assert await harness.envelope.decrypt(NIL_ORG_ID, blob, PLATFORM_AAD) == b"platform"
    with pytest.raises(DecryptionError):
        await fresh_envelope(harness).decrypt(harness.org, blob, PLATFORM_AAD)


async def test_platform_encrypt_needs_no_stored_key(harness: Harness) -> None:
    assert await harness.store.active_data_key(NIL_ORG_ID) is None
    before = harness.provider.generated
    first = EncryptedBlob.from_bytes(await harness.envelope.platform_encrypt(b"a", PLATFORM_AAD))
    second = EncryptedBlob.from_bytes(await harness.envelope.platform_encrypt(b"a", PLATFORM_AAD))
    assert harness.provider.generated == before + 2
    assert first.encrypted_data_key != second.encrypted_data_key


async def test_platform_decrypt_from_the_blob_alone(harness: Harness) -> None:
    blob = await harness.envelope.platform_encrypt(b"platform", PLATFORM_AAD)
    fresh = fresh_envelope(harness)
    decrypted = harness.provider.decrypted
    assert await fresh.platform_decrypt(blob, PLATFORM_AAD) == b"platform"
    assert harness.provider.decrypted == decrypted + 1
    assert await harness.envelope.platform_decrypt(blob, PLATFORM_AAD) == b"platform"
    assert harness.provider.decrypted == decrypted + 1
    with pytest.raises(DecryptionError):
        await fresh.platform_decrypt(blob, column_aad("users", "mfa_totp_secret_enc", ROW_B))


async def test_platform_str_round_trip(harness: Harness) -> None:
    blob = await harness.envelope.platform_encrypt_str("JBSWY3DPEHPK3PXP", PLATFORM_AAD)
    assert await harness.envelope.platform_decrypt_str(blob, PLATFORM_AAD) == "JBSWY3DPEHPK3PXP"
    invalid = await harness.envelope.platform_encrypt(b"\xff\xfe", PLATFORM_AAD)
    with pytest.raises(DecryptionError, match="UTF-8"):
        await harness.envelope.platform_decrypt_str(invalid, PLATFORM_AAD)


async def test_cache_avoids_repeated_unwraps(harness: Harness) -> None:
    assert (harness.provider.generated, harness.provider.decrypted) == (1, 0)
    blob = await harness.envelope.encrypt(harness.org, b"v", AAD)
    await harness.envelope.decrypt(harness.org, blob, AAD)
    assert harness.provider.decrypted == 0
    envelope = fresh_envelope(harness)
    await envelope.decrypt(harness.org, blob, AAD)
    await envelope.decrypt(harness.org, blob, AAD)
    await envelope.encrypt(harness.org, b"w", AAD)
    assert harness.provider.decrypted == 1
