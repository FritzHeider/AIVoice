"""The OAuth 2.0 authorization-code requests of the OIDC login, built with authlib (specs/04 §5).

authlib's RFC 6749 and RFC 7636 primitives build the authorization URL (PKCE ``S256``) and the token request
(``client_secret_basic``). The token request travels through :class:`va_api.auth.oidc_discovery.OidcDiscovery`
so it obeys the outbound-request guards; authlib's own httpx integration is not used (it is deprecated in
favour of ``httpx2`` and warns on import).
"""

from __future__ import annotations

from typing import Final

from authlib.oauth2.auth import ClientAuth
from authlib.oauth2.rfc6749.parameters import prepare_grant_uri, prepare_token_request
from authlib.oauth2.rfc7636 import create_s256_code_challenge

__all__ = ["CODE_CHALLENGE_METHOD", "SSO_SCOPE", "TokenRequest", "authorization_url", "token_request"]

SSO_SCOPE: Final = "openid email profile"
CODE_CHALLENGE_METHOD: Final = "S256"
_AUTH_METHOD: Final = "client_secret_basic"

type TokenRequest = tuple[str, dict[str, str]]


def authorization_url(  # noqa: PLR0913 - keyword-only OAuth parameters
    endpoint: str,
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    nonce: str,
    code_verifier: str,
    login_hint: str,
) -> str:
    """The provider's authorization URL for the code flow with PKCE ``S256``, ``state``, and ``nonce``."""
    url: str = prepare_grant_uri(
        endpoint,
        client_id,
        "code",
        redirect_uri=redirect_uri,
        scope=SSO_SCOPE,
        state=state,
        nonce=nonce,
        code_challenge=create_s256_code_challenge(code_verifier),
        code_challenge_method=CODE_CHALLENGE_METHOD,
        login_hint=login_hint,
    )
    return url


def token_request(
    *, client_id: str, client_secret: str, code: str, redirect_uri: str, code_verifier: str
) -> TokenRequest:
    """The form body and headers (HTTP Basic client authentication) exchanging ``code`` for tokens."""
    body: str = prepare_token_request(
        "authorization_code", code=code, redirect_uri=redirect_uri, code_verifier=code_verifier
    )
    auth = ClientAuth(client_id, client_secret, _AUTH_METHOD)
    _, headers, prepared = auth.prepare("POST", "", {}, body)
    return str(prepared), {str(name): str(value) for name, value in headers.items()}
