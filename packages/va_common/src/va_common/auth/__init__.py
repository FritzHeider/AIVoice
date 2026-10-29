"""Authentication primitives shared by services; pure functions plus thread-offloaded hashing, no network I/O.

- :mod:`va_common.auth.api_keys`: API key format and peppered hashing (specs/04-auth-tenancy-rbac.md §2.1).
- :mod:`va_common.auth.passwords`: argon2id password hashing and the password policy (specs/04 §4.2).
- :mod:`va_common.auth.tokens`: opaque session, CSRF, and email-link tokens (specs/04 §4.3, §4.4).
"""

from __future__ import annotations

from va_common.auth.api_keys import (
    API_KEY_LENGTH,
    API_KEY_PREFIX_LENGTH,
    API_KEY_PREFIXES,
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
from va_common.auth.passwords import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    SPEC_ARGON2_PARAMS,
    Argon2Params,
    PasswordHasher,
    check_password_policy,
    check_password_policy_async,
    password_policy_violation,
)
from va_common.auth.tokens import (
    TOKEN_BYTES,
    TOKEN_LENGTH,
    IssuedToken,
    generate_token,
    issue_token,
    looks_like_token,
    tokens_equal,
)

__all__ = [
    "API_KEY_LENGTH",
    "API_KEY_PREFIXES",
    "API_KEY_PREFIX_LENGTH",
    "API_KEY_RE",
    "BASE62",
    "MAX_PASSWORD_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "SPEC_ARGON2_PARAMS",
    "TOKEN_BYTES",
    "TOKEN_LENGTH",
    "ApiKeyHasher",
    "Argon2Params",
    "IssuedToken",
    "KeyEnvironment",
    "PasswordHasher",
    "Pepper",
    "api_key_prefix",
    "check_password_policy",
    "check_password_policy_async",
    "environment_of",
    "generate_api_key",
    "generate_token",
    "issue_token",
    "looks_like_api_key",
    "looks_like_token",
    "password_policy_violation",
    "sha256_hex",
    "tokens_equal",
]
