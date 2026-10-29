"""API key format and peppered hashing (specs/04-auth-tenancy-rbac.md §2.1; specs/28-security.md §2)."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any

import pytest

from va_common.auth import (
    API_KEY_LENGTH,
    API_KEY_RE,
    BASE62,
    ApiKeyHasher,
    KeyEnvironment,
    Pepper,
    api_key_prefix,
    environment_of,
    generate_api_key,
    looks_like_api_key,
    sha256_hex,
)


def _pepper(version: int = 1) -> Pepper:
    return Pepper(secrets.token_bytes(32), version)


class _Spy:
    """Wraps a callable and counts calls."""

    def __init__(self, target: Any) -> None:
        self.target = target
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return self.target(*args, **kwargs)


@pytest.fixture
def sha_spy(monkeypatch: pytest.MonkeyPatch) -> _Spy:
    spy = _Spy(hashlib.sha256)
    monkeypatch.setattr(hashlib, "sha256", spy)
    return spy


@pytest.mark.parametrize("environment", ["live", "test"])
def test_generate_api_key_shape(environment: KeyEnvironment) -> None:
    key = generate_api_key(environment)
    assert len(key) == API_KEY_LENGTH
    assert API_KEY_RE.fullmatch(key)
    assert key.startswith(f"va_{environment}_")
    assert set(key[8:]) <= set(BASE62)
    assert environment_of(key) == environment


def test_generate_api_key_rejects_unknown_environment() -> None:
    with pytest.raises(ValueError, match="unknown key environment"):
        generate_api_key("prod")  # type: ignore[arg-type]


def test_generated_keys_are_unique() -> None:
    assert len({generate_api_key("live") for _ in range(1000)}) == 1000


def test_prefix_is_first_12_chars() -> None:
    key = generate_api_key("test")
    assert api_key_prefix(key) == key[:12]
    assert api_key_prefix(key).startswith("va_test_")
    assert len(api_key_prefix(key)) == 12


@pytest.mark.parametrize(
    "value",
    [
        "va_prod_" + "a" * 32,
        "va_live_" + "a" * 31,
        "va_live_" + "a" * 33,
        "sk_live_" + "a" * 32,
        "va_live_" + "a" * 31 + "-",
        "Bearer va_live_" + "a" * 32,
        "",
    ],
)
def test_looks_like_api_key_rejects(value: str) -> None:
    assert not looks_like_api_key(value)
    with pytest.raises(ValueError, match="not an API key"):
        environment_of(value)


def test_hash_matches_spec_formula() -> None:
    pepper_hex = secrets.token_hex(32)
    hasher = ApiKeyHasher(Pepper.from_hex(pepper_hex, 1))
    key = generate_api_key("live")
    expected = hashlib.sha256(bytes.fromhex(pepper_hex) + key.encode("ascii")).hexdigest()
    assert hasher.hash(key) == expected
    assert hasher.hash(key, version=1) == expected
    assert hasher.verify(key, expected, 1)


def test_hash_unknown_version_raises() -> None:
    with pytest.raises(LookupError, match="no pepper configured for version 3"):
        ApiKeyHasher(_pepper()).hash("x", version=3)


def test_verify_uses_compare_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _Spy(hmac.compare_digest)
    monkeypatch.setattr(hmac, "compare_digest", spy)
    hasher = ApiKeyHasher(_pepper())
    key = generate_api_key("live")
    assert hasher.verify(key, hasher.hash(key), 1)
    assert spy.calls == 1
    assert not hasher.verify(key, "0" * 64, 1)
    assert spy.calls == 2


def test_verify_previous_pepper_for_older_version() -> None:
    old, new = _pepper(1), _pepper(2)
    key = generate_api_key("live")
    old_hash = ApiKeyHasher(old).hash(key)
    rotated = ApiKeyHasher(new, old)
    assert rotated.current_version == 2
    assert rotated.verify(key, old_hash, 1)
    assert not rotated.verify(key, old_hash, 2)
    assert rotated.verify(key, rotated.hash(key), 2)
    assert rotated.hash(key, version=1) == old_hash


def test_unknown_version_fails_with_one_hash(sha_spy: _Spy) -> None:
    hasher = ApiKeyHasher(_pepper(2))
    key = generate_api_key("live")
    # Even the hash the current pepper would produce fails for a version without a pepper.
    assert not hasher.verify(key, hasher.hash(key), 1)
    assert sha_spy.calls == 2


def test_previous_pepper_must_be_one_version_older() -> None:
    with pytest.raises(ValueError, match="version current - 1"):
        ApiKeyHasher(_pepper(3), _pepper(1))


def test_dummy_verify_hashes_once(sha_spy: _Spy) -> None:
    ApiKeyHasher(_pepper()).dummy_verify("va_live_whatever")
    assert sha_spy.calls == 1


@pytest.mark.parametrize("hex_value", ["ab" * 31, "ab" * 33, "zz" * 32, ""])
def test_pepper_from_hex_rejects_wrong_input(hex_value: str) -> None:
    with pytest.raises(ValueError, match="pepper must be"):
        Pepper.from_hex(hex_value, 1)


def test_pepper_rejects_version_zero() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        Pepper(secrets.token_bytes(32), 0)


def test_pepper_repr_hides_value() -> None:
    pepper = Pepper.from_hex(secrets.token_hex(32), 4)
    assert pepper.value.hex() not in repr(pepper)
    assert repr(pepper) == "Pepper(version=4)"


def test_sha256_hex() -> None:
    assert sha256_hex("abc") == hashlib.sha256(b"abc").hexdigest()
