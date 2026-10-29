"""OIDC ID token validation with joserfc (specs/04-auth-tenancy-rbac.md §5 step 3).

The signature is checked against the provider's JWKS with asymmetric algorithms only (never ``none`` or
``HS*``), then ``iss`` (the provider's exact discovery spelling), ``aud``/``azp``, ``exp``/``iat`` (60 s clock
skew), and ``nonce`` (constant time). ``email`` must be a string; ``email_verified`` is read as a boolean or
the strings ``"true"``/``"false"``. Every failure is an :class:`IdTokenError` whose ``reason`` is for logs
only.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

__all__ = [
    "ALLOWED_ALGORITHMS",
    "CLOCK_SKEW",
    "IdTokenClaims",
    "IdTokenError",
    "validate_id_token",
]

ALLOWED_ALGORITHMS: Final = (
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
    "ES512",
)
CLOCK_SKEW: Final = timedelta(seconds=60)
_TRUE: Final = frozenset({True, "true"})
_FALSE: Final = frozenset({False, "false"})


@dataclass(frozen=True, slots=True)
class IdTokenClaims:
    """The validated claims the login uses; ``email_verified`` is ``None`` when the provider omits it."""

    subject: str
    email: str
    email_verified: bool | None
    name: str


class IdTokenError(Exception):
    """The ID token is not acceptable; ``reason`` names the failed check (never shown to the user)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _key_set(jwks: Mapping[str, Any]) -> KeySet:
    try:
        return KeySet.import_key_set(dict(jwks))  # type: ignore[arg-type]
    except (JoseError, ValueError, TypeError, KeyError) as exc:
        raise IdTokenError("jwks") from exc


def _decode(id_token: str, keys: KeySet) -> dict[str, Any]:
    try:
        token = jwt.decode(id_token, keys, algorithms=list(ALLOWED_ALGORITHMS))
    except (JoseError, ValueError, TypeError) as exc:
        raise IdTokenError("signature") from exc
    return dict(token.claims)


def _check_issuer(claims: Mapping[str, Any], issuer: str) -> None:
    if claims.get("iss") != issuer:
        raise IdTokenError("iss")


def _check_audience(claims: Mapping[str, Any], client_id: str) -> None:
    audience = claims.get("aud")
    if isinstance(audience, str):
        audience = [audience]
    if not isinstance(audience, list) or client_id not in audience:
        raise IdTokenError("aud")
    if len(audience) > 1 and claims.get("azp") != client_id:
        raise IdTokenError("azp")


def _timestamp(claims: Mapping[str, Any], name: str) -> datetime:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise IdTokenError(name)
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise IdTokenError(name) from exc


def _check_times(claims: Mapping[str, Any], now: datetime) -> None:
    if _timestamp(claims, "exp") <= now - CLOCK_SKEW:
        raise IdTokenError("exp")
    if _timestamp(claims, "iat") > now + CLOCK_SKEW:
        raise IdTokenError("iat")


def _check_nonce(claims: Mapping[str, Any], nonce: str) -> None:
    presented = claims.get("nonce")
    if not isinstance(presented, str) or not hmac.compare_digest(presented.encode(), nonce.encode()):
        raise IdTokenError("nonce")


def _email_verified(claims: Mapping[str, Any]) -> bool | None:
    if "email_verified" not in claims:
        return None
    value = claims["email_verified"]
    if not isinstance(value, bool | str):
        raise IdTokenError("email_verified")
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise IdTokenError("email_verified")


def _string(claims: Mapping[str, Any], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value:
        raise IdTokenError(name)
    return value


def validate_id_token(  # noqa: PLR0913 - keyword-only validation inputs
    id_token: str,
    *,
    jwks: Mapping[str, Any],
    issuer: str,
    client_id: str,
    nonce: str,
    now: datetime,
) -> IdTokenClaims:
    """The claims of ``id_token`` after every check; :class:`IdTokenError` on the first failure."""
    claims = _decode(id_token, _key_set(jwks))
    _check_issuer(claims, issuer)
    _check_audience(claims, client_id)
    _check_times(claims, now)
    _check_nonce(claims, nonce)
    name = claims.get("name")
    return IdTokenClaims(
        subject=_string(claims, "sub"),
        email=_string(claims, "email"),
        email_verified=_email_verified(claims),
        name=name if isinstance(name, str) else "",
    )
