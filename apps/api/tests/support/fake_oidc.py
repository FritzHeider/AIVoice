"""A fake OIDC identity provider for respx (specs/04-auth-tenancy-rbac.md §5).

:class:`FakeOidcProvider` serves the discovery document, the JWKS, and a token endpoint that checks the client
credentials (HTTP Basic), the grant, the code, the ``redirect_uri``, and the PKCE verifier against the
``code_challenge`` of the authorization URL the test "visited" with :meth:`FakeOidcProvider.authorize`. The
token endpoint answers with an ID token signed by :data:`SIGNING_KEY` (RSA keys are generated once per test
process because generation is slow). :func:`configure_sso` creates an org with a connection through
``PUT /v1/org/sso``; :func:`sso_login` drives a browser through start, the provider, and the callback.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import respx
from fastapi import FastAPI
from identity_db import IdentityDb, unique_email
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey
from session_helpers import (
    API_BASE_URL,
    DASHBOARD_URL,
    Member,
    SessionContainer,
    login,
    make_member,
    session_app,
    write_headers,
)

from va_api.auth.cookies import SESSION_COOKIE
from va_api.auth.oidc_discovery import DISCOVERY_PATH
from va_db.models import MemberRole

__all__ = [
    "CALLBACK",
    "CLIENT_ID",
    "CLIENT_SECRET",
    "CODE",
    "ISSUER",
    "OTHER_KEY",
    "SIGNING_KEY",
    "START",
    "FakeOidcProvider",
    "SsoOrg",
    "configure_sso",
    "discovery_document",
    "id_token",
    "jwks_of",
    "login_error",
    "mocked_idp",
    "new_domain",
    "session_cookie_set",
    "sso_app",
    "sso_login",
]

ISSUER = "https://login.acme-dental.test"
CLIENT_ID = "voiceagents-dashboard"
CLIENT_SECRET = "idp-client-credential-7f3a"  # noqa: S105 - test client secret
CODE = "code-1"
START = "/internal/auth/sso/start"
CALLBACK = "/internal/auth/sso/callback"
SIGNING_KEY = RSAKey.generate_key(2048, parameters={"kid": "test-key"})
# Same kid, different key material: a token signed with it fails signature verification.
OTHER_KEY = RSAKey.generate_key(2048, parameters={"kid": "test-key"})


def new_domain() -> str:
    """A domain no other test of the module (sharing one database) claims."""
    return f"c{uuid.uuid4().hex[:10]}.acme-dental.test"


def discovery_document(issuer: str) -> dict[str, str]:
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/authorize",
        "token_endpoint": f"{issuer}/token",
        "jwks_uri": f"{issuer}/jwks",
    }


def jwks_of(*keys: RSAKey) -> dict[str, Any]:
    """The public JWKS of ``keys``."""
    return dict(KeySet(list(keys)).as_dict(private=False))


def id_token(
    claims: dict[str, Any], *, key: RSAKey = SIGNING_KEY, alg: str = "RS256", kid: str | None = "test-key"
) -> str:
    """``claims`` signed with ``key``; claims whose value is ``None`` are left out."""
    header: dict[str, Any] = {"alg": alg}
    if kid is not None:
        header["kid"] = kid
    payload = {name: value for name, value in claims.items() if value is not None}
    return jwt.encode(header, payload, key, algorithms=[alg])


def _s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


@dataclass
class FakeOidcProvider:
    """One provider. ``claims`` override the next ID token's defaults (``None`` removes a claim)."""

    now: Callable[[], datetime]
    issuer: str = ISSUER
    client_id: str = CLIENT_ID
    client_secret: str = CLIENT_SECRET
    claims: dict[str, Any] = field(default_factory=dict)
    key: RSAKey = field(default_factory=lambda: SIGNING_KEY)
    token_status: int = 200
    authorized: dict[str, str] = field(default_factory=dict)
    token_calls: int = 0

    def install(self, router: respx.MockRouter) -> None:
        router.get(f"{self.issuer}{DISCOVERY_PATH}").respond(json=discovery_document(self.issuer))
        router.get(f"{self.issuer}/jwks").respond(json=jwks_of(SIGNING_KEY))
        router.post(f"{self.issuer}/token").mock(side_effect=self._token)

    def authorize(self, location: str) -> str:
        """Record the authorization request at ``location`` (a redirect target) and return its ``state``."""
        parts = urlsplit(location)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{self.issuer}/authorize", location
        self.authorized = {name: values[0] for name, values in parse_qs(parts.query).items()}
        return self.authorized["state"]

    def next_id_token(self) -> str:
        now = self.now()
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "aud": self.client_id,
            "sub": "idp-user-1",
            "email": self.authorized.get("login_hint"),
            "email_verified": True,
            "name": "Sam Rivera",
            "nonce": self.authorized.get("nonce"),
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
        }
        return id_token({**claims, **self.claims}, key=self.key)

    def _client_ok(self, request: httpx.Request) -> bool:
        expected = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        return bool(request.headers.get("authorization") == f"Basic {expected}")

    def _grant_ok(self, form: dict[str, str]) -> bool:
        verifier = form.get("code_verifier", "")
        return (
            form.get("grant_type") == "authorization_code"
            and form.get("code") == CODE
            and form.get("redirect_uri") == self.authorized.get("redirect_uri")
            and _s256(verifier) == self.authorized.get("code_challenge")
        )

    def _token(self, request: httpx.Request) -> httpx.Response:
        self.token_calls += 1
        form = {name: values[0] for name, values in parse_qs(request.content.decode()).items()}
        if self.token_status != 200 or not self._client_ok(request) or not self._grant_ok(form):
            return httpx.Response(
                self.token_status if self.token_status != 200 else 400, json={"error": "invalid_grant"}
            )
        return httpx.Response(
            200, json={"access_token": "at-1", "token_type": "Bearer", "id_token": self.next_id_token()}
        )


@dataclass(frozen=True, slots=True)
class SsoOrg:
    """An org (with its owner) whose SSO connection lists ``domain``."""

    owner: Member
    domain: str

    def email(self, local: str = "sam") -> str:
        """A fresh address in the connection's domain."""
        return f"{unique_email(local).split('@')[0]}@{self.domain}"


async def configure_sso(
    client: httpx.AsyncClient,
    identity: IdentityDb,
    container: SessionContainer,
    *,
    default_role: MemberRole = "developer",
    enforce: bool = False,
) -> SsoOrg:
    """An owner signs in, creates the org's connection, and the client's cookies are cleared again."""
    owner = await make_member(identity, container.password_hasher)
    await login(client, owner.user.email)
    domain = new_domain()
    body = {
        "issuer": ISSUER,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "allowed_domains": [domain],
        "default_role": default_role,
        "enforce": enforce,
    }
    response = await client.put("/v1/org/sso", json=body, headers=write_headers(client))
    assert response.status_code == 200, response.text
    client.cookies.clear()
    return SsoOrg(owner=owner, domain=domain)


async def sso_login(
    client: httpx.AsyncClient, provider: FakeOidcProvider, email: str, **callback: str
) -> httpx.Response:
    """Start a login for ``email``, "visit" the provider, and call back with ``code`` (or ``callback``)."""
    start = await client.get(START, params={"email": email})
    assert start.status_code == 302, start.text
    state = provider.authorize(start.headers["location"])
    return await client.get(CALLBACK, params={"state": state, "code": CODE, **callback})


def login_error(reason: str) -> str:
    """The dashboard login page URL of a failed SSO login."""
    return f"{DASHBOARD_URL}/login?sso_error={reason}"


def session_cookie_set(response: httpx.Response) -> bool:
    return any(item.startswith(f"{SESSION_COOKIE}=") for item in response.headers.get_list("set-cookie"))


@contextmanager
def mocked_idp(provider: FakeOidcProvider) -> Iterator[respx.MockRouter]:
    """A respx router serving ``provider`` (unmatched requests fail)."""
    with respx.mock(assert_all_called=False) as router:
        provider.install(router)
        yield router


def sso_app(container: SessionContainer) -> FastAPI:
    """The session app with ``OIDC_ENABLED=true`` and the API served at :data:`API_BASE_URL`."""
    return session_app(container, oidc_enabled=True, public_api_base_url=API_BASE_URL)
