"""SSRF-safe client with DNS pinning (specs/28-security.md §6.4, specs/19-events-and-webhooks.md §8)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager

import httpcore
import httpx
import pytest
from dns_fakes import MappedResolver
from service_helpers import serving, settings_of
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from va_common.net import UnsafeDestinationError
from va_common.safe_http import GuardedBackend, safe_client, safe_transport
from va_common.settings import BaseServiceSettings

# One sample address per range of specs/19 §8 and the specs/28 §6.4 extras.
BLOCKED = [
    "10.1.2.3",
    "172.16.5.4",
    "192.168.1.1",
    "127.0.0.1",
    "169.254.169.254",
    "169.254.10.10",
    "::1",
    "fc00::1",
    "fe80::1",
    "100.64.0.1",
    "0.0.0.0",  # noqa: S104 - a refused address, nothing binds to it
    "192.0.0.8",
    "198.18.0.1",
    "240.0.0.1",
    "ff02::1",
    "224.0.0.1",
    "::ffff:10.0.0.1",
]


class CountingBackend(httpcore.AsyncNetworkBackend):
    """Inner backend that records connects and delegates to the real one."""

    def __init__(self) -> None:
        self.connects: list[tuple[str, int]] = []
        self._real = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore interface
        local_address: str | None = None,
        socket_options: Iterable[object] | None = None,  # noqa: ARG002 - httpcore interface
    ) -> httpcore.AsyncNetworkStream:
        self.connects.append((host, port))
        return await self._real.connect_tcp(host, port, timeout=timeout, local_address=local_address)

    async def sleep(self, seconds: float) -> None:
        await self._real.sleep(seconds)


@asynccontextmanager
async def receiver() -> AsyncIterator[tuple[int, list[str]]]:
    """A plain HTTP server on 127.0.0.1 recording each request's ``Host`` header."""
    hosts: list[str] = []

    async def echo(request: Request) -> PlainTextResponse:
        hosts.append(request.headers["host"])
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/{path:path}", echo, methods=["GET", "POST"])])
    async with serving(app) as server:
        yield int(server.url.rsplit(":", 1)[1]), hosts


def guarded(inner: CountingBackend, resolver: MappedResolver, *, allow_private: bool) -> httpx.AsyncClient:
    transport = safe_transport(allow_private=allow_private, resolver=resolver, inner=inner)
    return httpx.AsyncClient(transport=transport, timeout=5.0, follow_redirects=False)


async def test_connections_are_pinned_to_the_vetted_address_and_keep_the_name() -> None:
    inner, resolver = CountingBackend(), MappedResolver({"hooks.test": ["127.0.0.1"]})
    async with receiver() as (port, hosts), guarded(inner, resolver, allow_private=True) as client:
        response = await client.post(f"http://hooks.test:{port}/x", content=b"{}")
    assert response.status_code == 200
    assert hosts == [f"hooks.test:{port}"]
    assert inner.connects == [("127.0.0.1", port)]
    assert resolver.lookups == ["hooks.test"]


@pytest.mark.parametrize("address", BLOCKED)
async def test_every_blocked_range_is_refused_before_connecting(address: str) -> None:
    inner, resolver = CountingBackend(), MappedResolver({"evil.test": [address]})
    async with guarded(inner, resolver, allow_private=False) as client:
        with pytest.raises(UnsafeDestinationError) as info:
            await client.get("http://evil.test/")
    assert info.value.reason == "address"
    assert inner.connects == []


async def test_ip_literals_are_checked_without_dns() -> None:
    inner, resolver = CountingBackend(), MappedResolver({})
    async with guarded(inner, resolver, allow_private=False) as client:
        with pytest.raises(UnsafeDestinationError):
            await client.get("http://10.0.0.1/")
        with pytest.raises(UnsafeDestinationError):
            await client.get("http://[fd00:ec2::254]/")
    assert resolver.lookups == []
    assert inner.connects == []


async def test_mixed_answers_and_resolution_failures_are_refused() -> None:
    inner = CountingBackend()
    resolver = MappedResolver(
        {"mixed.test": ["8.8.8.8", "10.0.0.7"], "empty.test": [], "junk.test": ["nope"]}
    )
    async with guarded(inner, resolver, allow_private=False) as client:
        reasons = []
        for host in ("mixed.test", "empty.test", "junk.test", "missing.test"):
            with pytest.raises(UnsafeDestinationError) as info:
                await client.get(f"http://{host}/")
            reasons.append(info.value.reason)
    assert reasons == ["address", "resolution", "resolution", "resolution"]
    assert inner.connects == []


async def test_metadata_stays_blocked_where_private_targets_are_allowed() -> None:
    inner, resolver = CountingBackend(), MappedResolver({"meta.test": ["169.254.169.254"]})
    async with guarded(inner, resolver, allow_private=True) as client:
        with pytest.raises(UnsafeDestinationError):
            await client.get("http://meta.test/")
    assert inner.connects == []


async def test_a_hanging_resolver_times_out_as_resolution() -> None:
    release = asyncio.Event()

    async def hanging(host: str) -> list[str]:
        await release.wait()
        return [host]

    backend = GuardedBackend(allow_private=True, resolver=hanging, dns_timeout_s=0.05)
    with pytest.raises(UnsafeDestinationError) as info:
        await backend.vetted_address("slow.test")
    assert info.value.reason == "resolution"


async def test_unix_sockets_are_refused() -> None:
    backend = GuardedBackend(allow_private=True)
    with pytest.raises(UnsafeDestinationError):
        await backend.connect_unix_socket("/var/run/docker.sock")
    await backend.sleep(0)


def test_the_transport_pool_uses_the_guarded_backend() -> None:
    """Guards the private httpx attribute ``safe_transport`` replaces (httpx 0.28 has no backend argument)."""
    transport = safe_transport(allow_private=False)
    pool = transport._pool
    assert isinstance(pool, httpcore.AsyncConnectionPool)
    assert isinstance(pool._network_backend, GuardedBackend)


async def test_safe_client_never_follows_redirects() -> None:
    client = safe_client(settings_of(BaseServiceSettings), timeout=1.0, allow_private=True)
    async with client:
        assert client.follow_redirects is False
        assert isinstance(client._transport, httpx.AsyncHTTPTransport)
