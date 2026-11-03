"""Outbound destination guard shared by webhooks, tools, and fetches (specs/19-events-and-webhooks.md §8).

:func:`check_destination_url` vets a customer-supplied URL before anything connects to it: the scheme
must be ``https`` (or ``http`` when the caller allows it), the URL needs a host, and every address the
host resolves to must pass :func:`blocked_address`. It returns the vetted addresses so a pinning client
can connect to exactly those (specs/28 §6.4). Rules: specs/19 §8 Decision P05-T05a.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from typing import Final, Literal
from urllib.parse import urlsplit

__all__ = [
    "DNS_TIMEOUT_S",
    "METADATA_ADDRESSES",
    "HostResolver",
    "IpAddress",
    "UnsafeDestinationError",
    "UnsafeReason",
    "blocked_address",
    "check_destination_url",
    "parse_ip",
    "resolve_host",
    "system_resolver",
]

type IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
type HostResolver = Callable[[str], Awaitable[list[str]]]
type UnsafeReason = Literal["scheme", "host", "resolution", "address"]

DNS_TIMEOUT_S: Final = 2.0
# Cloud metadata endpoints stay blocked even where private targets are allowed (local/test).
METADATA_ADDRESSES: Final[frozenset[IpAddress]] = frozenset(
    {ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254")}
)


class UnsafeDestinationError(Exception):
    """The URL must not be contacted; ``reason`` says which rule refused it."""

    def __init__(self, reason: UnsafeReason) -> None:
        super().__init__(f"destination rejected: {reason}")
        self.reason: UnsafeReason = reason


async def system_resolver(host: str) -> list[str]:
    """The unique addresses ``host`` resolves to (A and AAAA), in resolver order, IPv6 zone ids removed."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]).split("%", 1)[0] for info in infos))


def parse_ip(host: str) -> IpAddress | None:
    """``host`` as an IP address (``[v6]`` brackets accepted), or ``None`` when it is a name."""
    try:
        return ipaddress.ip_address(host.removeprefix("[").removesuffix("]"))
    except ValueError:
        return None


def blocked_address(address: IpAddress, *, allow_private: bool = False) -> bool:
    """Whether no outbound request may reach ``address``.

    IPv4-mapped IPv6 addresses are judged by their embedded IPv4 address. Metadata, multicast, reserved
    (except loopback), and unspecified addresses are always blocked; anything else that is not globally
    routable is blocked unless ``allow_private``.
    """
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address in METADATA_ADDRESSES:
        return True
    if address.is_multicast or address.is_unspecified:
        return True
    # IPv6 ``::1`` sits inside the reserved ``::/8``; loopback follows the private rule instead.
    if address.is_reserved and not address.is_loopback:
        return True
    return not allow_private and not address.is_global


async def resolve_host(
    host: str, resolver: HostResolver, timeout_s: float = DNS_TIMEOUT_S
) -> list[IpAddress]:
    """Every address ``host`` resolves to within ``timeout_s``; :class:`UnsafeDestinationError` ``resolution``
    on a resolver error, the timeout, an empty answer, or an answer that is not an IP address."""
    try:
        async with asyncio.timeout(timeout_s):
            answers = await resolver(host)
    except (OSError, TimeoutError) as exc:
        raise UnsafeDestinationError("resolution") from exc
    addresses = [parsed for answer in answers if (parsed := parse_ip(answer)) is not None]
    if not addresses or len(addresses) != len(answers):
        raise UnsafeDestinationError("resolution")
    return addresses


async def check_destination_url(
    url: str,
    *,
    allow_http: bool,
    allow_private: bool,
    resolver: HostResolver = system_resolver,
    timeout_s: float = DNS_TIMEOUT_S,
) -> tuple[IpAddress, ...]:
    """The addresses ``url`` may be contacted at; :class:`UnsafeDestinationError` when any rule refuses it.

    IP-literal hosts are checked without DNS; names are resolved once and every answer must pass.
    """
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
    except ValueError as exc:
        raise UnsafeDestinationError("host") from exc
    schemes = {"https", "http"} if allow_http else {"https"}
    if parts.scheme.lower() not in schemes:
        raise UnsafeDestinationError("scheme")
    if not host:
        raise UnsafeDestinationError("host")
    literal = parse_ip(host)
    addresses = [literal] if literal is not None else await resolve_host(host, resolver, timeout_s)
    if any(blocked_address(address, allow_private=allow_private) for address in addresses):
        raise UnsafeDestinationError("address")
    return tuple(addresses)
