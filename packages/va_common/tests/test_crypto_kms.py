from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any, cast

import aioboto3
import pytest
from botocore.exceptions import ClientError
from crypto_fakes import FakeKmsClient
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

from va_common.crypto import (
    LOCAL_KEK_ID,
    AwsKmsProvider,
    CryptoError,
    DataKey,
    DecryptionError,
    LocalKmsProvider,
    WrappedDataKey,
    aws_kms_provider,
    open_kms_provider,
)
from va_common.settings import CryptoSettings, KmsProviderName


def local_provider() -> LocalKmsProvider:
    return LocalKmsProvider(os.urandom(32))


async def test_local_wrap_unwrap_round_trip() -> None:
    provider = local_provider()
    org = uuid.uuid4()
    data_key = await provider.generate_data_key(org)
    assert data_key.kek_id == LOCAL_KEK_ID
    assert len(data_key.plaintext) == 32
    assert len(data_key.encrypted_data_key) == 12 + 32 + 16
    assert data_key.plaintext not in data_key.encrypted_data_key
    assert await provider.decrypt_data_key(org, data_key.wrapped) == data_key.plaintext


async def test_local_generates_distinct_keys() -> None:
    provider = local_provider()
    org = uuid.uuid4()
    first, second = await provider.generate_data_key(org), await provider.generate_data_key(org)
    assert first.plaintext != second.plaintext
    assert first.encrypted_data_key != second.encrypted_data_key


async def test_local_wrapped_key_is_bound_to_org() -> None:
    provider = local_provider()
    data_key = await provider.generate_data_key(uuid.uuid4())
    with pytest.raises(DecryptionError):
        await provider.decrypt_data_key(uuid.uuid4(), data_key.wrapped)


async def test_local_wrong_master_key() -> None:
    org = uuid.uuid4()
    data_key = await local_provider().generate_data_key(org)
    with pytest.raises(DecryptionError):
        await local_provider().decrypt_data_key(org, data_key.wrapped)


@pytest.mark.parametrize("index", [0, 11, 12, 30, -1])
async def test_local_tampered_wrapped_key(index: int) -> None:
    provider = local_provider()
    org = uuid.uuid4()
    data_key = await provider.generate_data_key(org)
    tampered = bytearray(data_key.encrypted_data_key)
    tampered[index] ^= 0x01
    with pytest.raises(DecryptionError):
        await provider.decrypt_data_key(org, WrappedDataKey(LOCAL_KEK_ID, bytes(tampered)))


async def test_local_short_wrapped_key() -> None:
    with pytest.raises(DecryptionError, match="malformed"):
        await local_provider().decrypt_data_key(uuid.uuid4(), WrappedDataKey(LOCAL_KEK_ID, b"\x00" * 27))


async def test_local_foreign_kek_id() -> None:
    provider = local_provider()
    org = uuid.uuid4()
    data_key = await provider.generate_data_key(org)
    with pytest.raises(DecryptionError, match="different key-encryption key"):
        await provider.decrypt_data_key(org, WrappedDataKey("local:v2", data_key.encrypted_data_key))


async def test_local_custom_kek_id_and_wrong_length_payload() -> None:
    master = os.urandom(32)
    provider = LocalKmsProvider(master, kek_id="local:v2")
    org = uuid.uuid4()
    data_key = await provider.generate_data_key(org)
    assert data_key.kek_id == "local:v2"
    # A wrapped value that authenticates but holds 16 bytes instead of a 32-byte data key.
    nonce = os.urandom(12)
    short = nonce + AESGCM(master).encrypt(nonce, os.urandom(16), f"org_id={org}".encode())
    with pytest.raises(DecryptionError, match="wrong length"):
        await provider.decrypt_data_key(org, WrappedDataKey("local:v2", short))


def test_local_from_hex() -> None:
    key = os.urandom(32)
    provider = LocalKmsProvider.from_hex(key.hex())
    assert isinstance(provider, LocalKmsProvider)


@pytest.mark.parametrize("value", ["zz" * 32, "ab" * 31, "ab" * 33, "", "abc"])
def test_local_from_hex_rejects_bad_keys(value: str) -> None:
    with pytest.raises(CryptoError) as info:
        LocalKmsProvider.from_hex(value)
    if value:
        assert value not in str(info.value)


@pytest.mark.parametrize("size", [0, 16, 31, 33])
def test_local_rejects_wrong_master_key_length(size: int) -> None:
    with pytest.raises(CryptoError, match="32 bytes"):
        LocalKmsProvider(b"\x00" * size)


def test_data_key_repr_hides_plaintext() -> None:
    plaintext = os.urandom(32)
    data_key = DataKey(kek_id="local:v1", plaintext=plaintext, encrypted_data_key=b"\x01")
    assert repr(plaintext) not in repr(data_key)
    assert "plaintext" not in repr(data_key)


async def test_aws_generate_passes_key_spec_and_context() -> None:
    client = FakeKmsClient()
    provider = AwsKmsProvider(client, "alias/voiceagents")
    org = uuid.uuid4()
    data_key = await provider.generate_data_key(org)
    assert data_key.kek_id == client.key_arn
    assert len(data_key.plaintext) == 32
    assert client.calls == [
        (
            "GenerateDataKey",
            {"KeyId": "alias/voiceagents", "KeySpec": "AES_256", "EncryptionContext": {"org_id": str(org)}},
        )
    ]
    assert await provider.decrypt_data_key(org, data_key.wrapped) == data_key.plaintext
    assert client.calls[-1] == (
        "Decrypt",
        {"KeyId": client.key_arn, "EncryptionContext": {"org_id": str(org)}},
    )


async def test_aws_wrong_org_raises_decryption_error() -> None:
    provider = AwsKmsProvider(FakeKmsClient(), "alias/voiceagents")
    data_key = await provider.generate_data_key(uuid.uuid4())
    with pytest.raises(DecryptionError):
        await provider.decrypt_data_key(uuid.uuid4(), data_key.wrapped)


async def test_aws_foreign_key_raises_decryption_error() -> None:
    provider = AwsKmsProvider(FakeKmsClient(), "alias/voiceagents")
    org = uuid.uuid4()
    data_key = await provider.generate_data_key(org)
    with pytest.raises(DecryptionError):
        await provider.decrypt_data_key(
            org, replace(data_key.wrapped, kek_id="arn:aws:kms:us-east-1:1:key/x")
        )


async def test_aws_other_client_errors_propagate() -> None:
    client = FakeKmsClient()
    provider = AwsKmsProvider(client, "alias/voiceagents")
    org = uuid.uuid4()
    data_key = await provider.generate_data_key(org)
    client.decrypt_error = "AccessDeniedException"
    with pytest.raises(ClientError):
        await provider.decrypt_data_key(org, data_key.wrapped)


async def test_aws_generate_wrong_length_key() -> None:
    provider = AwsKmsProvider(FakeKmsClient(data_key_size=16), "alias/voiceagents")
    with pytest.raises(CryptoError, match="wrong length"):
        await provider.generate_data_key(uuid.uuid4())


async def test_aws_decrypt_wrong_length_key() -> None:
    client = FakeKmsClient()
    provider = AwsKmsProvider(client, "alias/voiceagents")
    org = uuid.uuid4()
    data_key = await provider.generate_data_key(org)
    client.decrypt_plaintext = os.urandom(16)
    with pytest.raises(DecryptionError, match="wrong length"):
        await provider.decrypt_data_key(org, data_key.wrapped)


class MalformedKmsClient:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response

    async def generate_data_key(self, **_: Any) -> dict[str, Any]:
        return self.response

    async def decrypt(self, **_: Any) -> dict[str, Any]:
        return self.response


@pytest.mark.parametrize(
    "response",
    [
        {"KeyId": "arn", "CiphertextBlob": b"x"},
        {"KeyId": "arn", "Plaintext": "not-bytes", "CiphertextBlob": b"x"},
        {"Plaintext": b"\x00" * 32, "CiphertextBlob": b"x"},
        {"KeyId": "", "Plaintext": b"\x00" * 32, "CiphertextBlob": b"x"},
        {"KeyId": "arn", "Plaintext": b"\x00" * 32},
    ],
)
async def test_aws_malformed_responses(response: dict[str, Any]) -> None:
    provider = AwsKmsProvider(MalformedKmsClient(response), "alias/voiceagents")
    with pytest.raises(CryptoError, match="KMS response field"):
        await provider.generate_data_key(uuid.uuid4())


async def test_aws_decrypt_missing_plaintext() -> None:
    provider = AwsKmsProvider(MalformedKmsClient({"KeyId": "arn"}), "alias/voiceagents")
    with pytest.raises(CryptoError, match="Plaintext"):
        await provider.decrypt_data_key(uuid.uuid4(), WrappedDataKey("arn", b"x"))


@pytest.mark.parametrize("key_id", ["", "   "])
def test_aws_requires_key_id(key_id: str) -> None:
    with pytest.raises(CryptoError, match="AWS_KMS_KEY_ID"):
        AwsKmsProvider(FakeKmsClient(), key_id)


class FakeSession:
    def __init__(self, client: FakeKmsClient) -> None:
        self.kms = client
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def client(self, service_name: str, **kwargs: Any) -> Any:
        self.calls.append((service_name, kwargs))

        @asynccontextmanager
        async def manager() -> AsyncIterator[FakeKmsClient]:
            yield self.kms

        return manager()


async def test_aws_kms_provider_opens_client_with_timeouts() -> None:
    session = FakeSession(FakeKmsClient())
    async with aws_kms_provider(
        "alias/voiceagents", region_name="eu-west-1", session=cast("aioboto3.Session", session)
    ) as provider:
        data_key = await provider.generate_data_key(uuid.uuid4())
    assert data_key.kek_id == session.kms.key_arn
    [(service, kwargs)] = session.calls
    assert service == "kms"
    assert kwargs["region_name"] == "eu-west-1"
    config = kwargs["config"]
    assert (config.connect_timeout, config.read_timeout) == (5, 10)
    assert config.retries == {"max_attempts": 3, "mode": "standard"}


async def test_aws_kms_provider_default_session(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession(FakeKmsClient())
    monkeypatch.setattr(aioboto3, "Session", lambda: session)
    async with aws_kms_provider("alias/voiceagents") as provider:
        assert isinstance(provider, AwsKmsProvider)
    assert session.calls[0][1]["region_name"] is None


def crypto_settings(**values: Any) -> CryptoSettings:
    return CryptoSettings(**{"_env_file": None, **values})


async def test_open_kms_provider_local() -> None:
    settings = crypto_settings(local_master_key=SecretStr(os.urandom(32).hex()))
    async with open_kms_provider(settings) as provider:
        assert isinstance(provider, LocalKmsProvider)
        org = uuid.uuid4()
        data_key = await provider.generate_data_key(org)
        assert await provider.decrypt_data_key(org, data_key.wrapped) == data_key.plaintext


async def test_open_kms_provider_local_requires_master_key() -> None:
    with pytest.raises(RuntimeError, match="LOCAL_MASTER_KEY is not configured"):
        async with open_kms_provider(crypto_settings()):
            pytest.fail("the provider must not open without LOCAL_MASTER_KEY")


async def test_open_kms_provider_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession(FakeKmsClient())
    monkeypatch.setattr(aioboto3, "Session", lambda: session)
    settings = crypto_settings(kms_provider=KmsProviderName.AWS, aws_kms_key_id="alias/voiceagents")
    async with open_kms_provider(settings) as provider:
        assert isinstance(provider, AwsKmsProvider)
        await provider.generate_data_key(uuid.uuid4())
    assert session.kms.calls[0][1]["KeyId"] == "alias/voiceagents"
