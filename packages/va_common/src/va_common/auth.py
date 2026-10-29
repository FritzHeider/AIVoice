"""API key format and peppered hashing (specs/04-auth-tenancy-rbac.md §2.1; specs/28-security.md §2).

Pure functions, no I/O. A key is ``va_live_`` or ``va_test_`` plus 32 base62 characters; its ``prefix`` (first
12 characters) is the lookup handle and ``secret_hash`` is ``sha256(pepper || plaintext)`` hex. Peppers are
versioned (``api_keys.pepper_version``): the current pepper verifies keys of its version, the previous pepper
keys of the version before it. Every verification performs exactly one hash, including the dummy hash done for
an unknown prefix, so response timing does not reveal which prefixes exist.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import string
from dataclasses import dataclass, field
from typing import Final, Literal, Self, get_args

__all__ = [
    "API_KEY_LENGTH",
    "API_KEY_PREFIXES",
    "API_KEY_PREFIX_LENGTH",
    "API_KEY_RE",
    "BASE62",
    "ApiKeyHasher",
    "KeyEnvironment",
    "Pepper",
    "api_key_prefix",
    "environment_of",
    "generate_api_key",
    "looks_like_api_key",
    "sha256_hex",
]

type KeyEnvironment = Literal["live", "test"]

API_KEY_PREFIXES: Final = ("va_live_", "va_test_")
API_KEY_LENGTH: Final = 40
API_KEY_PREFIX_LENGTH: Final = 12
BASE62: Final = string.digits + string.ascii_uppercase + string.ascii_lowercase
API_KEY_RE: Final = re.compile(r"va_(?:live|test)_[0-9A-Za-z]{32}")

_SECRET_LENGTH: Final = 32
_PEPPER_BYTES: Final = 32
_DUMMY_HASH: Final = "0" * 64
_ENVIRONMENTS: Final[tuple[str, ...]] = get_args(KeyEnvironment.__value__)


def generate_api_key(environment: KeyEnvironment) -> str:
    """A fresh plaintext key: ``va_<environment>_`` + 32 base62 characters from ``secrets.choice``."""
    if environment not in _ENVIRONMENTS:
        msg = f"unknown key environment: {environment!r}"
        raise ValueError(msg)
    secret = "".join(secrets.choice(BASE62) for _ in range(_SECRET_LENGTH))
    return f"va_{environment}_{secret}"


def looks_like_api_key(value: str) -> bool:
    """Whether ``value`` has the §2.1 shape (prefix, length 40, base62 secret)."""
    return API_KEY_RE.fullmatch(value) is not None


def api_key_prefix(plaintext: str) -> str:
    """The stored and displayed ``prefix``: the first 12 characters (``va_live_3kQ9``)."""
    return plaintext[:API_KEY_PREFIX_LENGTH]


def environment_of(plaintext: str) -> KeyEnvironment:
    """``live`` or ``test`` from a well-formed key; ``ValueError`` otherwise."""
    if not looks_like_api_key(plaintext):
        msg = "not an API key"
        raise ValueError(msg)
    return "live" if plaintext.startswith(API_KEY_PREFIXES[0]) else "test"


def sha256_hex(value: str) -> str:
    """Hex SHA-256 of ``value`` (UTF-8); the primitive for stored token hashes."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Pepper:
    """A 32-byte pepper and the ``pepper_version`` it is stored under; ``repr`` never shows the value."""

    value: bytes = field(repr=False)
    version: int

    def __post_init__(self) -> None:
        if len(self.value) != _PEPPER_BYTES:
            msg = f"pepper must be {_PEPPER_BYTES} bytes"
            raise ValueError(msg)
        if self.version < 1:
            msg = "pepper version must be at least 1"
            raise ValueError(msg)

    @classmethod
    def from_hex(cls, hex_value: str, version: int) -> Self:
        """Parse 64 hex characters; ``ValueError`` for any other input."""
        try:
            raw = bytes.fromhex(hex_value)
        except ValueError:
            msg = "pepper must be hex"
            raise ValueError(msg) from None
        return cls(raw, version)


class ApiKeyHasher:
    """Hashes and verifies API keys with the current pepper and, optionally, the previous one."""

    def __init__(self, current: Pepper, previous: Pepper | None = None) -> None:
        if previous is not None and previous.version != current.version - 1:
            msg = "the previous pepper must have version current - 1"
            raise ValueError(msg)
        self._current = current
        self._peppers = {current.version: current}
        if previous is not None:
            self._peppers[previous.version] = previous

    @property
    def current_version(self) -> int:
        """The ``pepper_version`` stored with newly created keys."""
        return self._current.version

    def hash(self, plaintext: str, *, version: int | None = None) -> str:
        """``sha256(pepper || plaintext)`` hex; ``LookupError`` for a version without a configured pepper."""
        selected = self._current.version if version is None else version
        pepper = self._peppers.get(selected)
        if pepper is None:
            msg = f"no pepper configured for version {selected}"
            raise LookupError(msg)
        return _digest(pepper, plaintext)

    def verify(self, plaintext: str, secret_hash: str, version: int) -> bool:
        """Constant-time check against ``secret_hash``; an unknown version still hashes once and fails."""
        pepper = self._peppers.get(version)
        candidate = _digest(pepper if pepper is not None else self._current, plaintext)
        matches = hmac.compare_digest(candidate, secret_hash)
        return matches and pepper is not None

    def dummy_verify(self, plaintext: str) -> None:
        """One hash and comparison whose result is discarded (unknown prefix, malformed key)."""
        hmac.compare_digest(_digest(self._current, plaintext), _DUMMY_HASH)


def _digest(pepper: Pepper, plaintext: str) -> str:
    return hashlib.sha256(pepper.value + plaintext.encode("utf-8")).hexdigest()
