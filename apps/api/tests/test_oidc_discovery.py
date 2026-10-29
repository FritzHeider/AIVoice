"""OIDC discovery fetch and its outbound guards (specs/04-auth-tenancy-rbac.md §5; specs/28 §6.4)."""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx
from api_helpers import make_settings

from va_api.auth.oidc_discovery import (
    ADDRESS_NOT_ALLOWED,
    DISCOVERY_PATH,
    INSECURE_URL,
    MAX_DISCOVERY_BYTES,
    NOT_JSON_OBJECT,
    DiscoveryError,
    OidcDiscovery,
    ProviderMetadata,
    discovery_for,
    normalise_issuer,
    same_issuer,
    system_resolver,
)
from va_common.settings import Environment

type Http = httpx.AsyncClient
type Router = respx.MockRouter

ISSUER = "https://login.acme-dental.test"
URL = f"{ISSUER}{DISCOVERY_PATH}"


def document(issuer: str = ISSUER, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/authorize",
        "token_endpoint": f"{issuer}/oauth/token",
        "jwks_uri": f"{issuer}/.well-known/jwks.json",
    }
    body.update(overrides)
    return {key: value for key, value in body.items() if value is not None}


def resolver_of(*addresses: str) -> Any:
    async def resolve(_host: str) -> list[str]:
        return list(addresses)

    return resolve


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


async def fetch_error(discovery: OidcDiscovery, issuer: str = ISSUER) -> str:
    with pytest.raises(DiscoveryError) as caught:
        await discovery.fetch(issuer)
    return caught.value.reason


async def test_good_document(http: Http, mock: Router) -> None:
    route = mock.get(URL).respond(json=document())
    discovery = OidcDiscovery(http, allow_insecure=False, resolver=resolver_of("93.184.216.34"))
    metadata = await discovery.fetch(ISSUER)
    assert metadata == ProviderMetadata(
        issuer=ISSUER,
        authorization_endpoint=f"{ISSUER}/authorize",
        token_endpoint=f"{ISSUER}/oauth/token",
        jwks_uri=f"{ISSUER}/.well-known/jwks.json",
    )
    assert route.call_count == 1
    assert route.calls.last.request.headers["accept"] == "application/json"


async def test_provider_issuer_with_trailing_slash_is_kept(http: Http, mock: Router) -> None:
    mock.get(URL).respond(json=document(issuer=f"{ISSUER}/", token_endpoint=f"{ISSUER}/token"))
    metadata = await OidcDiscovery(http, allow_insecure=True).fetch(ISSUER)
    assert metadata.issuer == f"{ISSUER}/"


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx.Response(200, json=document(issuer="https://evil.test")), "issuer mismatch"),
        (httpx.Response(200, json=document(authorization_endpoint=None)), "missing authorization_endpoint"),
        (httpx.Response(200, json=document(token_endpoint=None)), "missing token_endpoint"),
        (httpx.Response(200, json=document(jwks_uri="")), "missing jwks_uri"),
        (httpx.Response(200, json=document(jwks_uri=42)), "missing jwks_uri"),
        (httpx.Response(200, text="<html>login</html>"), "not a JSON object"),
        (httpx.Response(200, json=[document()]), "not a JSON object"),
        (httpx.Response(500, json={}), "HTTP 500"),
        (httpx.Response(204), "HTTP 204"),
        (httpx.Response(302, headers={"Location": "http://169.254.169.254/"}), "redirect not allowed"),
        (httpx.Response(200, content=b" " * (MAX_DISCOVERY_BYTES + 1)), "response too large"),
        (httpx.ConnectTimeout("slow"), "timed out"),
        (httpx.ConnectError("refused"), "unreachable"),
    ],
)
async def test_invalid_responses(
    http: Http, mock: Router, response: httpx.Response | Exception, reason: str
) -> None:
    mock.get(URL).mock(side_effect=[response])
    assert await fetch_error(OidcDiscovery(http, allow_insecure=True)) == reason


async def test_streamed_body_over_the_cap_is_too_large(http: Http, mock: Router) -> None:
    chunks = [b"{" + b" " * 65_536] * 5
    mock.get(URL).respond(200, stream=httpx.ByteStream(b"".join(chunks)), headers={"content-length": ""})
    assert await fetch_error(OidcDiscovery(http, allow_insecure=True)) == "response too large"


async def test_redirect_is_not_followed(http: Http, mock: Router) -> None:
    target = mock.get("https://elsewhere.test/config").respond(json=document())
    mock.get(URL).respond(301, headers={"Location": "https://elsewhere.test/config"})
    assert await fetch_error(OidcDiscovery(http, allow_insecure=True)) == "redirect not allowed"
    assert not target.called


@pytest.mark.parametrize("address", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "::1", "fd00::7"])
async def test_private_addresses_are_refused(http: Http, mock: Router, address: str) -> None:
    route = mock.get(URL).respond(json=document())
    discovery = OidcDiscovery(http, allow_insecure=False, resolver=resolver_of("93.184.216.34", address))
    assert await fetch_error(discovery) == "address not allowed"
    assert not route.called


async def test_ip_literal_and_resolution_failures(http: Http, mock: Router) -> None:
    async def failing(_host: str) -> list[str]:
        raise socket.gaierror

    mock.get(f"https://10.1.2.3{DISCOVERY_PATH}").respond(json=document("https://10.1.2.3"))
    strict = OidcDiscovery(http, allow_insecure=False, resolver=failing)
    assert await fetch_error(strict, "https://10.1.2.3") == "address not allowed"
    assert await fetch_error(strict) == "unreachable"
    empty = OidcDiscovery(http, allow_insecure=False, resolver=resolver_of())
    assert await fetch_error(empty) == "address not allowed"


async def test_plain_http_only_when_insecure_is_allowed(http: Http, mock: Router) -> None:
    issuer = "http://idp.local.test:8080"
    mock.get(f"{issuer}{DISCOVERY_PATH}").respond(json=document(issuer))
    strict = OidcDiscovery(http, allow_insecure=False, resolver=resolver_of("93.184.216.34"))
    assert await fetch_error(strict, issuer) == "insecure issuer"
    assert (await OidcDiscovery(http, allow_insecure=True).fetch(issuer)).issuer == issuer


async def test_insecure_endpoint_in_a_https_document(http: Http, mock: Router) -> None:
    mock.get(URL).respond(json=document(token_endpoint="http://login.acme-dental.test/token"))  # noqa: S106
    strict = OidcDiscovery(http, allow_insecure=False, resolver=resolver_of("93.184.216.34"))
    assert await fetch_error(strict) == "insecure endpoint token_endpoint"
    mock.get(URL).respond(json=document(jwks_uri="ftp://login.acme-dental.test/jwks"))
    assert await fetch_error(strict) == "insecure endpoint jwks_uri"
    mock.get(URL).respond(json=document(jwks_uri="https://[login.acme-dental.test/jwks"))
    assert await fetch_error(strict) == "insecure endpoint jwks_uri"


async def test_system_resolver_resolves_localhost() -> None:
    addresses = await system_resolver("localhost")
    assert addresses
    assert len(addresses) == len(set(addresses))
    assert {"127.0.0.1", "::1"} & set(addresses)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (ISSUER, ISSUER),
        (f"  {ISSUER}/ ", ISSUER),
        (f"{ISSUER}/tenant/v2.0/", f"{ISSUER}/tenant/v2.0"),
        ("http://localhost:9400", "http://localhost:9400"),
    ],
)
def test_normalise_issuer(raw: str, expected: str) -> None:
    assert normalise_issuer(raw) == expected


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("login.acme-dental.test", "absolute https URL"),
        ("ftp://login.acme-dental.test", "absolute https URL"),
        ("https://", "absolute https URL"),
        ("https://admin:pw@login.acme-dental.test", "credentials"),
        (f"{ISSUER}?tenant=1", "query or fragment"),
        (f"{ISSUER}#frag", "query or fragment"),
        ("https://login.acme-dental.test:99999", "invalid port"),
    ],
)
def test_normalise_issuer_rejects(raw: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        normalise_issuer(raw)


def test_same_issuer_ignores_one_trailing_slash() -> None:
    assert same_issuer(ISSUER, f"{ISSUER}/")
    assert not same_issuer(ISSUER, "https://login.acme-dental.test.evil.test")


@pytest.mark.parametrize(("va_env", "insecure"), [("local", True), ("test", True), ("staging", False)])
async def test_discovery_for_environment(http: httpx.AsyncClient, va_env: str, insecure: bool) -> None:
    # model_copy skips the validators (staging would also need a TLS SMTP_URL and the AWS KMS provider).
    settings = make_settings().model_copy(update={"va_env": Environment(va_env)})
    assert discovery_for(settings, http).allow_insecure is insecure


async def guarded_error(call: Any) -> str:
    with pytest.raises(DiscoveryError) as caught:
        await call
    return caught.value.reason


async def test_fetch_json_and_check_url(http: Http, mock: Router) -> None:
    jwks_url = f"{ISSUER}/jwks"
    mock.get(jwks_url).respond(json={"keys": []})
    discovery = OidcDiscovery(http, allow_insecure=False, resolver=resolver_of("93.184.216.34"))
    assert await discovery.fetch_json(jwks_url) == {"keys": []}
    await discovery.check_url(jwks_url)
    assert await guarded_error(discovery.fetch_json("http://login.acme-dental.test/jwks")) == INSECURE_URL
    assert await guarded_error(discovery.check_url("not a url")) == INSECURE_URL
    mock.get(f"{ISSUER}/list").respond(json=[1, 2])
    assert await guarded_error(discovery.fetch_json(f"{ISSUER}/list")) == NOT_JSON_OBJECT
    private = OidcDiscovery(http, allow_insecure=False, resolver=resolver_of("10.0.0.7"))
    assert await guarded_error(private.fetch_json(jwks_url)) == ADDRESS_NOT_ALLOWED
    assert mock.calls.call_count == 2


async def test_post_form_sends_the_form_and_needs_200(http: Http, mock: Router) -> None:
    token_url = f"{ISSUER}/token"
    route = mock.post(token_url).respond(json={"id_token": "t"})
    discovery = OidcDiscovery(http, allow_insecure=True)
    result = await discovery.post_form(
        token_url, "grant_type=authorization_code", {"Authorization": "Basic x"}
    )
    assert result == {"id_token": "t"}
    sent = route.calls.last.request
    assert sent.content == b"grant_type=authorization_code"
    assert sent.headers["content-type"] == "application/x-www-form-urlencoded"
    assert sent.headers["authorization"] == "Basic x"
    assert sent.headers["accept"] == "application/json"
    route.respond(400, json={"error": "invalid_grant"})
    assert await guarded_error(discovery.post_form(token_url, "", {})) == "HTTP 400"
    route.respond(302, headers={"Location": "https://elsewhere.test/"})
    assert await guarded_error(discovery.post_form(token_url, "", {})) == "redirect not allowed"
