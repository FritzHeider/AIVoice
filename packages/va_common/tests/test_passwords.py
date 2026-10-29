"""argon2id hashing and the password policy (specs/04-auth-tenancy-rbac.md §4.2)."""

from __future__ import annotations

import base64
import gzip
import threading
from importlib import resources

import pytest
from argon2 import PasswordHasher as Argon2Hasher

from va_common.auth import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    SPEC_ARGON2_PARAMS,
    Argon2Params,
    PasswordHasher,
    check_password_policy,
    check_password_policy_async,
    password_policy_violation,
)
from va_common.auth import passwords as passwords_module
from va_common.auth.passwords import COMMON_PASSWORDS_RESOURCE, common_passwords, email_local_part
from va_common.errors import AppError

CHEAP = Argon2Params(time_cost=1, memory_cost=8, parallelism=1, hash_len=16, salt_len=8)
STRONG = "violet-harbour-lantern-42"


@pytest.fixture(scope="module")
def hasher() -> PasswordHasher:
    return PasswordHasher(CHEAP)


def _b64decode(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4))


def test_spec_argon2_params() -> None:
    assert (
        Argon2Params(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16)
        == SPEC_ARGON2_PARAMS
    )
    spec = PasswordHasher()
    encoded = spec.hash(STRONG)
    assert encoded.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    salt, digest = encoded.split("$")[4:6]
    assert len(_b64decode(salt)) == 16
    assert len(_b64decode(digest)) == 32
    assert spec.verify(encoded, STRONG)
    assert spec.params is SPEC_ARGON2_PARAMS


def test_hash_verify_roundtrip(hasher: PasswordHasher) -> None:
    encoded = hasher.hash(STRONG)
    assert encoded.startswith("$argon2id$")
    assert hasher.verify(encoded, STRONG)
    assert hasher.hash(STRONG) != encoded


def test_verify_wrong_password_false(hasher: PasswordHasher) -> None:
    assert not hasher.verify(hasher.hash(STRONG), STRONG + "x")


@pytest.mark.parametrize("malformed", ["", "not-a-hash", "$argon2id$v=19$m=8,t=1,p=1$broken", "$2b$12$abc"])
def test_verify_malformed_hash_false(hasher: PasswordHasher, malformed: str) -> None:
    assert not hasher.verify(malformed, STRONG)


def test_verify_none_hash_runs_dummy_verify(monkeypatch: pytest.MonkeyPatch, hasher: PasswordHasher) -> None:
    calls: list[str] = []
    original = Argon2Hasher.verify

    def spy(self: Argon2Hasher, password_hash: str, password: str) -> bool:
        calls.append(password_hash)
        return original(self, password_hash, password)

    monkeypatch.setattr(Argon2Hasher, "verify", spy)
    assert not hasher.verify(None, STRONG)
    assert len(calls) == 1
    assert calls[0].startswith("$argon2id$")


def test_needs_rehash_on_param_change(hasher: PasswordHasher) -> None:
    cheap_hash = hasher.hash(STRONG)
    stronger = PasswordHasher(
        Argon2Params(time_cost=2, memory_cost=16, parallelism=1, hash_len=16, salt_len=8)
    )
    assert stronger.needs_rehash(cheap_hash)
    assert not hasher.needs_rehash(cheap_hash)
    assert hasher.needs_rehash("not-a-hash")


async def test_async_wrappers_run_in_thread(monkeypatch: pytest.MonkeyPatch, hasher: PasswordHasher) -> None:
    loop_thread = threading.get_ident()
    threads: list[int] = []
    original_hash = PasswordHasher.hash
    original_verify = PasswordHasher.verify

    def hash_spy(self: PasswordHasher, password: str) -> str:
        threads.append(threading.get_ident())
        return original_hash(self, password)

    def verify_spy(self: PasswordHasher, password_hash: str | None, password: str) -> bool:
        threads.append(threading.get_ident())
        return original_verify(self, password_hash, password)

    monkeypatch.setattr(PasswordHasher, "hash", hash_spy)
    monkeypatch.setattr(PasswordHasher, "verify", verify_spy)
    encoded = await hasher.hash_async(STRONG)
    assert await hasher.verify_async(encoded, STRONG)
    assert not await hasher.verify_async(None, STRONG)
    assert len(threads) == 3
    assert loop_thread not in threads


@pytest.mark.parametrize(
    ("password", "email", "message"),
    [
        ("a" * 5 + "b" * 6, None, "Password must be at least 12 characters."),
        ("Zq7!kd" + "x" * 116 + "M2#pw9w", None, "Password must be at most 128 characters."),
        ("password1234", None, "Password is too common; choose a less predictable one."),
        ("PASSWORD1234", None, "Password is too common; choose a less predictable one."),
        (
            "\uff30\uff21\uff33\uff33\uff37\uff2f\uff32\uff24\uff11\uff12\uff13\uff14",
            None,
            "Password is too common; choose a less predictable one.",
        ),
        ("my-Jane.Doe-harbour", "jane.doe+x@example.com", "Password must not contain your email address."),
        ("violet-JANE-harbour", "Jane@example.com", "Password must not contain your email address."),
    ],
)
def test_policy_rejects(password: str, email: str | None, message: str) -> None:
    assert password_policy_violation(password, email=email) == message
    with pytest.raises(AppError) as excinfo:
        check_password_policy(password, email=email)
    assert excinfo.value.code == "PASSWORD_TOO_WEAK"
    assert excinfo.value.http_status == 400
    assert excinfo.value.field == "password"
    assert excinfo.value.message == message


@pytest.mark.parametrize(
    ("password", "email"),
    [
        ("Zq7!kd" + "x" * 3 + "M2#", None),
        ("Zq7!kd" + "x" * 116 + "M2#pw9", None),
        ("violet-harbour-lantern-42", "jane.doe@example.com"),
        ("al-violet-harbour", "al@example.com"),
        ("violet-harbour-lantern", None),
        ("violet-harbour-lantern", "not-an-email"),
    ],
)
def test_policy_accepts(password: str, email: str | None) -> None:
    assert password_policy_violation(password, email=email) is None
    check_password_policy(password, email=email)


def test_policy_length_bounds() -> None:
    assert (MIN_PASSWORD_LENGTH, MAX_PASSWORD_LENGTH) == (12, 128)
    # NFKC: the ligature "ﬃ" counts as three code points ("ffi").
    assert password_policy_violation("violet-ha" + "ﬃ", email=None) is None


def test_policy_rule_order_length_before_breached() -> None:
    assert password_policy_violation("password", email=None) == "Password must be at least 12 characters."


async def test_check_password_policy_async() -> None:
    await check_password_policy_async(STRONG, email="jane@example.com")
    with pytest.raises(AppError):
        await check_password_policy_async("password1234", email=None)


@pytest.mark.parametrize(
    ("email", "local"),
    [
        ("Jane.Doe+news@Example.com", "jane.doe"),
        ("odd@local@example.com", "odd@local"),
        ("noatsign", "noatsign"),
    ],
)
def test_email_local_part(email: str, local: str) -> None:
    assert email_local_part(email) == local


def test_common_passwords_bundled_resource() -> None:
    data = resources.files("va_common.auth").joinpath(COMMON_PASSWORDS_RESOURCE).read_bytes()
    lines = gzip.decompress(data).decode("utf-8").splitlines()
    assert len(lines) >= 90_000
    assert lines == sorted(set(lines))
    assert all(line and line == line.strip() and line == line.casefold() for line in lines)
    assert "password1234" in lines
    assert common_passwords() == frozenset(lines)


def test_common_passwords_loaded_once() -> None:
    assert passwords_module.common_passwords() is passwords_module.common_passwords()
