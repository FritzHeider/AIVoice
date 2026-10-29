"""Dashboard password hashing and policy (specs/04-auth-tenancy-rbac.md §4.2; specs/28-security.md §2).

Hashes are argon2id with the spec parameters. ``verify`` never raises for a bad or missing hash: a mismatch,
a malformed hash, and a user without a password (SSO-only, or unknown email) all return ``False``, and the
missing hash case still runs one argon2 verification so login timing does not tell them apart. argon2 is
CPU-bound (~50-100 ms at 64 MiB, t=3), so request handlers use the ``*_async`` variants, which run it in a
worker thread.

The policy rejects passwords outside 12-128 code points (after NFKC), passwords in the bundled top-100 000
breached list (case-insensitive), and passwords that contain the email local part.
"""

from __future__ import annotations

import asyncio
import functools
import gzip
import secrets
import unicodedata
from dataclasses import dataclass
from importlib import resources
from typing import Final

import argon2
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from va_common.errors import AppError

__all__ = [
    "COMMON_PASSWORDS_RESOURCE",
    "MAX_PASSWORD_LENGTH",
    "MIN_LOCAL_PART_CHECK",
    "MIN_PASSWORD_LENGTH",
    "SPEC_ARGON2_PARAMS",
    "Argon2Params",
    "PasswordHasher",
    "breached_list_entry",
    "check_password_policy",
    "check_password_policy_async",
    "common_passwords",
    "email_local_part",
    "normalise_password",
    "password_policy_violation",
]

MIN_PASSWORD_LENGTH: Final = 12
MAX_PASSWORD_LENGTH: Final = 128
MIN_LOCAL_PART_CHECK: Final = 3
COMMON_PASSWORDS_RESOURCE: Final = "data/common_passwords.txt.gz"

_MSG_TOO_SHORT: Final = f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
_MSG_TOO_LONG: Final = f"Password must be at most {MAX_PASSWORD_LENGTH} characters."
_MSG_BREACHED: Final = "Password is too common; choose a less predictable one."
_MSG_EMAIL: Final = "Password must not contain your email address."


@dataclass(frozen=True, slots=True)
class Argon2Params:
    """argon2id cost parameters; ``memory_cost`` is in KiB."""

    time_cost: int
    memory_cost: int
    parallelism: int
    hash_len: int
    salt_len: int


SPEC_ARGON2_PARAMS: Final = Argon2Params(
    time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16
)


class PasswordHasher:
    """argon2id hashing with verification that never raises and rehash detection.

    Construction computes one hash of a random value (the dummy hash used when a user has no password), so
    build one hasher per process and reuse it.
    """

    __slots__ = ("_dummy_hash", "_hasher", "_params")

    def __init__(self, params: Argon2Params = SPEC_ARGON2_PARAMS) -> None:
        self._params = params
        self._hasher = argon2.PasswordHasher(
            time_cost=params.time_cost,
            memory_cost=params.memory_cost,
            parallelism=params.parallelism,
            hash_len=params.hash_len,
            salt_len=params.salt_len,
            type=argon2.Type.ID,
        )
        self._dummy_hash = self._hasher.hash(secrets.token_urlsafe(32))

    @property
    def params(self) -> Argon2Params:
        return self._params

    def hash(self, password: str) -> str:
        """The encoded argon2id hash (``$argon2id$v=19$m=…,t=…,p=…$salt$hash``) with a fresh random salt."""
        return self._hasher.hash(password)

    def verify(self, password_hash: str | None, password: str) -> bool:
        """Whether ``password`` matches; ``False`` for a mismatch, a malformed hash, or ``None``.

        ``None`` (no password set) verifies against the dummy hash first, so it costs one argon2 run like a
        real check.
        """
        if password_hash is None:
            self._verify(self._dummy_hash, password)
            return False
        return self._verify(password_hash, password)

    def needs_rehash(self, password_hash: str) -> bool:
        """Whether the hash was made with other parameters (rehash on the next successful login).

        A malformed hash also needs rehashing.
        """
        try:
            return self._hasher.check_needs_rehash(password_hash)
        except (InvalidHashError, ValueError):
            return True

    async def hash_async(self, password: str) -> str:
        """:meth:`hash` in a worker thread."""
        return await asyncio.to_thread(self.hash, password)

    async def verify_async(self, password_hash: str | None, password: str) -> bool:
        """:meth:`verify` in a worker thread."""
        return await asyncio.to_thread(self.verify, password_hash, password)

    def _verify(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False


def normalise_password(value: str) -> str:
    """NFKC normal form: lengths are counted in it and the breached list is built from it."""
    return unicodedata.normalize("NFKC", value)


def breached_list_entry(value: str) -> str:
    """How a password appears in (and is looked up in) the breached list: NFKC, then casefolded."""
    return normalise_password(value).casefold()


@functools.cache
def common_passwords() -> frozenset[str]:
    """The bundled breached-password list (NFKC, casefolded), decompressed once per process.

    Blocking: async callers go through :func:`check_password_policy_async`.
    """
    data = resources.files("va_common.auth").joinpath(COMMON_PASSWORDS_RESOURCE).read_bytes()
    text = gzip.decompress(data).decode("utf-8")
    return frozenset(line for line in text.splitlines() if line)


def email_local_part(email: str) -> str:
    """Text before the last ``@`` minus any ``+tag``, casefolded: ``Jane.Doe+x@ex.com`` -> ``jane.doe``."""
    local = email.rpartition("@")[0] if "@" in email else email
    return normalise_password(local.partition("+")[0]).casefold()


def password_policy_violation(password: str, *, email: str | None) -> str | None:
    """The message of the first failing rule (length, breached list, email local part), or ``None``."""
    normalised = normalise_password(password)
    if len(normalised) < MIN_PASSWORD_LENGTH:
        return _MSG_TOO_SHORT
    if len(normalised) > MAX_PASSWORD_LENGTH:
        return _MSG_TOO_LONG
    folded = breached_list_entry(normalised)
    if folded in common_passwords():
        return _MSG_BREACHED
    if email is not None:
        local = email_local_part(email)
        if len(local) >= MIN_LOCAL_PART_CHECK and local in folded:
            return _MSG_EMAIL
    return None


def check_password_policy(password: str, *, email: str | None) -> None:
    """Raise ``AppError("PASSWORD_TOO_WEAK", …, field="password")`` when the policy rejects ``password``."""
    message = password_policy_violation(password, email=email)
    if message is not None:
        raise AppError("PASSWORD_TOO_WEAK", message, field="password")


async def check_password_policy_async(password: str, *, email: str | None) -> None:
    """:func:`check_password_policy` in a worker thread (the first call decompresses the breached list)."""
    await asyncio.to_thread(check_password_policy, password, email=email)
