"""Opaque session/CSRF/email tokens (specs/04-auth-tenancy-rbac.md §4.3, §4.4)."""

from __future__ import annotations

import base64
import re

from va_common.auth import TOKEN_BYTES, IssuedToken, generate_token, issue_token, sha256_hex, tokens_equal
from va_common.auth.tokens import TOKEN_LENGTH

_BASE64URL = re.compile(r"[A-Za-z0-9_-]+")


def test_generate_token_shape() -> None:
    token = generate_token()
    assert len(token) == TOKEN_LENGTH == 43
    assert _BASE64URL.fullmatch(token)
    assert len(base64.urlsafe_b64decode(token + "=")) == TOKEN_BYTES == 32


def test_generate_token_unique() -> None:
    assert len({generate_token() for _ in range(1000)}) == 1000


def test_issue_token_stores_sha256_hash() -> None:
    issued = issue_token()
    assert issued.token_hash == sha256_hex(issued.plaintext)
    assert len(issued.plaintext) == TOKEN_LENGTH


def test_issued_token_repr_hides_plaintext() -> None:
    issued = issue_token()
    assert issued.plaintext not in repr(issued)
    assert issued.token_hash in repr(issued)
    fixture = IssuedToken(plaintext="visible?", token_hash="h")  # noqa: S106  (literal fixture, not a secret)
    assert repr(fixture) == "IssuedToken(token_hash='h')"


def test_tokens_equal() -> None:
    token = generate_token()
    assert tokens_equal(token, token)
    assert not tokens_equal(token, generate_token())
    assert not tokens_equal(token, token[:-1])
    assert not tokens_equal("", token)
    assert tokens_equal("é", "é")
