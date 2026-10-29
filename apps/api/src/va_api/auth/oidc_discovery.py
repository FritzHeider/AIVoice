"""OIDC provider requests with outbound-request guards (specs/04-auth-tenancy-rbac.md §5; specs/28 §6.4).

:class:`OidcDiscovery` fetches ``{issuer}/.well-known/openid-configuration`` and returns the endpoints the
authorization-code flow needs; the SSO login also fetches the provider's JWKS
(:meth:`OidcDiscovery.fetch_json`) and exchanges the authorization code (:meth:`OidcDiscovery.post_form`)
through it. Every request never follows
redirects, gives up after 5 s, reads at most 256 KiB, and (outside ``VA_ENV=local``/``test``) refuses plain
``http`` and hosts that resolve to anything but global addresses. Every failure is a :class:`DiscoveryError`
whose ``reason`` is shown to the admin (or logged by the login flow).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit

import httpx

from va_api.settings import ApiSettings
from va_common.settings import Environment

__all__ = [
    "ADDRESS_NOT_ALLOWED",
    "DISCOVERY_PATH",
    "DISCOVERY_TIMEOUT_S",
    "ENDPOINT_FIELDS",
    "INSECURE_ISSUER",
    "INSECURE_URL",
    "ISSUER_MISMATCH",
    "MAX_DISCOVERY_BYTES",
    "NOT_JSON_OBJECT",
    "REDIRECT_NOT_ALLOWED",
    "TIMED_OUT",
    "TOO_LARGE",
    "UNREACHABLE",
    "DiscoveryError",
    "HostResolver",
    "OidcDiscovery",
    "ProviderMetadata",
    "discovery_for",
    "normalise_issuer",
    "same_issuer",
    "system_resolver",
]

DISCOVERY_PATH: Final = "/.well-known/openid-configuration"
DISCOVERY_TIMEOUT_S: Final = 5.0
MAX_DISCOVERY_BYTES: Final = 262_144
ENDPOINT_FIELDS: Final = ("authorization_endpoint", "token_endpoint", "jwks_uri")
# DiscoveryError reasons (the f-string ones: "HTTP <status>", "missing <field>", "insecure endpoint <field>").
INSECURE_ISSUER: Final = "insecure issuer"
INSECURE_URL: Final = "insecure url"
UNREACHABLE: Final = "unreachable"
TIMED_OUT: Final = "timed out"
REDIRECT_NOT_ALLOWED: Final = "redirect not allowed"
TOO_LARGE: Final = "response too large"
NOT_JSON_OBJECT: Final = "not a JSON object"
ISSUER_MISMATCH: Final = "issuer mismatch"
ADDRESS_NOT_ALLOWED: Final = "address not allowed"
_INSECURE_ENVIRONMENTS: Final = frozenset({Environment.LOCAL, Environment.TEST})
_SCHEMES: Final = frozenset({"http", "https"})
_HTTP_OK: Final = 200
_REDIRECTS: Final = range(300, 400)

type HostResolver = Callable[[str], Awaitable[list[str]]]


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    """The discovery document fields we use; ``issuer`` is the provider's own spelling (``iss`` checks)."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str


class DiscoveryError(Exception):
    """The issuer could not be validated; ``reason`` is a short, admin-facing explanation."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def system_resolver(host: str) -> list[str]:
    """The unique addresses ``host`` resolves to, in resolver order."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def normalise_issuer(raw: str) -> str:
    """``raw`` without surrounding whitespace and one trailing ``/``; ``ValueError`` unless it is an absolute
    ``http(s)`` URL with a host and no credentials, query, or fragment."""
    value = raw.strip()
    value = value.removesuffix("/")
    parts = urlsplit(value)
    if parts.scheme not in _SCHEMES or not parts.hostname:
        msg = "issuer must be an absolute https URL"
        raise ValueError(msg)
    if "@" in parts.netloc:
        msg = "issuer must not contain credentials"
        raise ValueError(msg)
    if "?" in value or "#" in value:
        msg = "issuer must not contain a query or fragment"
        raise ValueError(msg)
    try:
        parts.port  # noqa: B018 - parsing the port validates it
    except ValueError as exc:
        msg = "issuer has an invalid port"
        raise ValueError(msg) from exc
    return value


def same_issuer(left: str, right: str) -> bool:
    """Whether two issuer identifiers are equal, ignoring one trailing ``/`` on either."""
    return left.removesuffix("/") == right.removesuffix("/")


def _host_of(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    return parts.hostname


class OidcDiscovery:
    """Fetches and validates OIDC discovery documents through one shared ``httpx.AsyncClient``.

    ``allow_insecure`` (``VA_ENV`` ``local``/``test``) accepts ``http`` URLs and private addresses.
    """

    def __init__(
        self, http: httpx.AsyncClient, *, allow_insecure: bool, resolver: HostResolver = system_resolver
    ) -> None:
        self._http = http
        self._allow_insecure = allow_insecure
        self._resolver = resolver

    @property
    def allow_insecure(self) -> bool:
        return self._allow_insecure

    def _secure(self, url: str) -> bool:
        scheme = url.split(":", 1)[0].lower() if ":" in url else ""
        allowed = _SCHEMES if self._allow_insecure else {"https"}
        return scheme in allowed and bool(_host_of(url))

    async def _check_host(self, host: str) -> None:
        try:
            addresses = [host] if _is_ip(host) else await self._resolver(host)
        except OSError as exc:
            raise DiscoveryError(UNREACHABLE) from exc
        if not addresses or not all(ipaddress.ip_address(address).is_global for address in addresses):
            raise DiscoveryError(ADDRESS_NOT_ALLOWED)

    async def _send(
        self, method: str, url: str, *, body: str | None = None, headers: dict[str, str]
    ) -> bytes:
        try:
            async with self._http.stream(
                method,
                url,
                content=body,
                headers={"Accept": "application/json", **headers},
                follow_redirects=False,
                timeout=DISCOVERY_TIMEOUT_S,
            ) as response:
                if response.status_code in _REDIRECTS:
                    raise DiscoveryError(REDIRECT_NOT_ALLOWED)
                if response.status_code != _HTTP_OK:
                    status = f"HTTP {response.status_code}"
                    raise DiscoveryError(status)
                return await _read_capped(response)
        except httpx.TimeoutException as exc:
            raise DiscoveryError(TIMED_OUT) from exc
        except httpx.HTTPError as exc:
            raise DiscoveryError(UNREACHABLE) from exc

    def _metadata(self, issuer: str, body: bytes) -> ProviderMetadata:
        document = _json_object(body)
        declared = document.get("issuer")
        if not isinstance(declared, str) or not same_issuer(declared, issuer):
            raise DiscoveryError(ISSUER_MISMATCH)
        endpoints: dict[str, str] = {}
        for name in ENDPOINT_FIELDS:
            value = document.get(name)
            if not isinstance(value, str) or not value:
                missing = f"missing {name}"
                raise DiscoveryError(missing)
            if not self._secure(value):
                insecure = f"insecure endpoint {name}"
                raise DiscoveryError(insecure)
            endpoints[name] = value
        return ProviderMetadata(issuer=declared, **endpoints)

    async def _check(self, url: str, insecure_reason: str) -> None:
        if not self._secure(url):
            raise DiscoveryError(insecure_reason)
        if not self._allow_insecure:
            await self._check_host(_host_of(url) or "")

    async def fetch(self, issuer: str) -> ProviderMetadata:
        """The provider metadata of the normalised ``issuer``; :class:`DiscoveryError` on any failure."""
        await self._check(issuer, INSECURE_ISSUER)
        body = await self._send("GET", f"{issuer}{DISCOVERY_PATH}", headers={})
        return self._metadata(issuer, body)

    async def check_url(self, url: str) -> None:
        """:class:`DiscoveryError` unless ``url`` passes the scheme and address rules."""
        await self._check(url, INSECURE_URL)

    async def fetch_json(self, url: str) -> dict[str, Any]:
        """``GET url`` under the request guards; the response must be a JSON object."""
        await self.check_url(url)
        return _json_object(await self._send("GET", url, headers={}))

    async def post_form(self, url: str, body: str, headers: dict[str, str]) -> dict[str, Any]:
        """``POST url`` with a form-encoded ``body`` under the request guards; the response must be a JSON
        object (any status but 200 is a :class:`DiscoveryError`)."""
        await self.check_url(url)
        form_headers = {"Content-Type": "application/x-www-form-urlencoded", **headers}
        return _json_object(await self._send("POST", url, body=body, headers=form_headers))


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


async def _read_capped(response: httpx.Response) -> bytes:
    declared = response.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_DISCOVERY_BYTES:
        raise DiscoveryError(TOO_LARGE)
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > MAX_DISCOVERY_BYTES:
            raise DiscoveryError(TOO_LARGE)
    return bytes(body)


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        document = json.loads(body)
    except ValueError as exc:
        raise DiscoveryError(NOT_JSON_OBJECT) from exc
    if not isinstance(document, dict):
        raise DiscoveryError(NOT_JSON_OBJECT)
    return document


def discovery_for(settings: ApiSettings, http: httpx.AsyncClient) -> OidcDiscovery:
    """The discovery client for ``settings``: insecure URLs and addresses only in ``local`` and ``test``."""
    return OidcDiscovery(http, allow_insecure=settings.va_env in _INSECURE_ENVIRONMENTS)
