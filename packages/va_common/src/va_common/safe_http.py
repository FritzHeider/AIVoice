"""SSRF-safe outbound HTTP client with DNS pinning (specs/28-security.md §6.4, specs/19 §8).

:class:`GuardedBackend` is an ``httpcore`` network backend that vets every TCP connection before a socket
opens: IP-literal hosts are judged by :func:`va_common.net.blocked_address`; names are resolved once and
**every** answer must pass, then the connection goes to the first vetted address. Because the check runs at
connect time on the very address that is dialled, a DNS answer that changes between a pre-flight
:func:`va_common.net.check_destination_url` and the request (DNS rebinding) cannot reach a blocked network.
TLS SNI and the ``Host`` header stay the original name (``httpcore`` takes them from the request origin).

A refused connection raises :class:`va_common.net.UnsafeDestinationError` out of the request. Clients built
by :func:`safe_client` never follow redirects. Rules: specs/28 §6.4 Decision P06-T03.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Final

import httpcore
import httpx
from opentelemetry.trace import TracerProvider

from va_common.http import http_client
from va_common.net import (
    DNS_TIMEOUT_S,
    HostResolver,
    IpAddress,
    UnsafeDestinationError,
    blocked_address,
    parse_ip,
    resolve_host,
    system_resolver,
)
from va_common.settings import BaseServiceSettings

__all__ = ["KEEPALIVE_EXPIRY_S", "GuardedBackend", "safe_client", "safe_transport"]

KEEPALIVE_EXPIRY_S: Final = 5.0

type SocketOption = tuple[int, int, int] | tuple[int, int, bytes | bytearray] | tuple[int, int, None, int]


class GuardedBackend(httpcore.AsyncNetworkBackend):
    """Connects only to addresses :func:`blocked_address` lets through (vetted at connect time)."""

    def __init__(
        self,
        *,
        allow_private: bool,
        resolver: HostResolver = system_resolver,
        inner: httpcore.AsyncNetworkBackend | None = None,
        dns_timeout_s: float = DNS_TIMEOUT_S,
    ) -> None:
        self._allow_private = allow_private
        self._resolver = resolver
        self._inner = inner if inner is not None else httpcore.AnyIOBackend()
        self._dns_timeout_s = dns_timeout_s

    async def vetted_address(self, host: str) -> IpAddress:
        """The address a connection to ``host`` goes to; :class:`UnsafeDestinationError` when refused."""
        literal = parse_ip(host)
        addresses = (
            [literal]
            if literal is not None
            else await resolve_host(host, self._resolver, self._dns_timeout_s)
        )
        if any(blocked_address(address, allow_private=self._allow_private) for address in addresses):
            raise UnsafeDestinationError("address")
        return addresses[0]

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore's backend interface
        local_address: str | None = None,
        socket_options: Iterable[SocketOption] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        address = await self.vetted_address(host)
        return await self._inner.connect_tcp(
            str(address), port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )

    async def connect_unix_socket(
        self,
        path: str,  # noqa: ARG002 - httpcore's backend interface; Unix sockets are always refused
        timeout: float | None = None,  # noqa: ARG002, ASYNC109 - httpcore's backend interface
        socket_options: Iterable[SocketOption] | None = None,  # noqa: ARG002 - httpcore's backend interface
    ) -> httpcore.AsyncNetworkStream:
        raise UnsafeDestinationError("host")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def safe_transport(
    *,
    allow_private: bool,
    resolver: HostResolver = system_resolver,
    max_connections: int = 200,
    max_keepalive: int = 50,
    inner: httpcore.AsyncNetworkBackend | None = None,
) -> httpx.AsyncHTTPTransport:
    """An HTTP/1.1 transport whose connection pool dials through :class:`GuardedBackend` (no proxies)."""
    transport = httpx.AsyncHTTPTransport(
        limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_keepalive),
        trust_env=False,
    )
    # httpx 0.28 has no ``network_backend`` argument; the pool is rebuilt with the guarded backend.
    # ``test_safe_http`` pins the private ``_pool`` attribute so an httpx upgrade cannot silently skip this.
    transport._pool = httpcore.AsyncConnectionPool(
        ssl_context=httpx.create_ssl_context(trust_env=False),
        max_connections=max_connections,
        max_keepalive_connections=max_keepalive,
        keepalive_expiry=KEEPALIVE_EXPIRY_S,
        http1=True,
        http2=False,
        network_backend=GuardedBackend(allow_private=allow_private, resolver=resolver, inner=inner),
    )
    return transport


def safe_client(  # noqa: PLR0913 - one keyword per client knob keeps call sites explicit
    settings: BaseServiceSettings,
    *,
    timeout: float | httpx.Timeout,
    allow_private: bool,
    resolver: HostResolver = system_resolver,
    max_connections: int = 200,
    max_keepalive: int = 50,
    headers: Mapping[str, str] | None = None,
    tracer_provider: TracerProvider | None = None,
    version: str = "unknown",
) -> httpx.AsyncClient:
    """An instrumented client for customer-supplied URLs: pinned, guarded connections, no redirects."""
    return http_client(
        settings,
        timeout=timeout,
        headers=headers,
        max_connections=max_connections,
        max_keepalive=max_keepalive,
        transport=safe_transport(
            allow_private=allow_private,
            resolver=resolver,
            max_connections=max_connections,
            max_keepalive=max_keepalive,
        ),
        tracer_provider=tracer_provider,
        version=version,
    )
