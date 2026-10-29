"""Opaque bearer tokens for sessions, CSRF, and emailed links (specs/04-auth-tenancy-rbac.md §4.3, §4.4).

A token is 32 random bytes, base64url without padding (43 characters). Only ``sha256_hex(token)`` is stored;
the plaintext goes to the client once (cookie, response body, or email link).
"""

from __future__ import annotations

import base64
import hmac
import secrets
from dataclasses import dataclass, field
from typing import Final

from va_common.auth.api_keys import sha256_hex

__all__ = ["TOKEN_BYTES", "TOKEN_LENGTH", "IssuedToken", "generate_token", "issue_token", "tokens_equal"]

TOKEN_BYTES: Final = 32
TOKEN_LENGTH: Final = 43


def generate_token() -> str:
    """32 bytes from ``secrets.token_bytes``, base64url without ``=`` padding."""
    return base64.urlsafe_b64encode(secrets.token_bytes(TOKEN_BYTES)).rstrip(b"=").decode("ascii")


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """A fresh token and the hash to store; ``repr`` never shows the plaintext."""

    plaintext: str = field(repr=False)
    token_hash: str


def issue_token() -> IssuedToken:
    """A new token together with its stored ``sha256_hex`` hash."""
    plaintext = generate_token()
    return IssuedToken(plaintext=plaintext, token_hash=sha256_hex(plaintext))


def tokens_equal(presented: str, expected: str) -> bool:
    """Constant-time equality of two tokens (UTF-8 bytes), e.g. the CSRF header against its cookie."""
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))
