"""Outbound destination guard (specs/19-events-and-webhooks.md §8 Decision P05-T05a; specs/28 §6.4)."""

from __future__ import annotations

import asyncio
import ipaddress

import pytest

from va_common.net import (
    METADATA_ADDRESSES,
    HostResolver,
    UnsafeDestinationError,
    blocked_address,
    check_destination_url,
    parse_ip,
    system_resolver,
)

BLOCKED = [
    "10.0.0.1",
    "172.16.0.1",
    "172.31.255.255",
    "192.168.1.1",
    "127.0.0.1",
    "169.254.169.254",
    "169.254.1.1",
    "::1",
    "fc00::1",
    "fd12::1",
    "fe80::1",
    "100.64.0.1",
    "100.127.255.255",
    "0.0.0.1",
    "192.0.0.8",
    "198.18.0.1",
    "198.19.255.255",
    "240.0.0.1",
    "255.255.255.255",
    "224.0.0.1",
    "ff02::1",
    "::ffff:10.0.0.1",
    "::ffff:127.0.0.1",
    "::",
    "0.0.0.0",  # noqa: S104 - an address under test, not a bind
]
ALLOWED = ["8.8.8.8", "1.1.1.1", "172.32.0.1", "100.128.0.1", "2606:4700:4700::1111", "::ffff:8.8.8.8"]


def ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    return ipaddress.ip_address(value)


def resolver_of(answers: dict[str, list[str]]) -> HostResolver:
    async def resolve(host: str) -> list[str]:
        return answers[host]

    return resolve


async def never_resolve(host: str) -> list[str]:
    msg = f"resolver called for {host}"
    raise AssertionError(msg)


async def failing_resolver(host: str) -> list[str]:
    raise OSError(host)


async def hanging_resolver(host: str) -> list[str]:
    await asyncio.Event().wait()
    return [host]


async def reject_reason(url: str, resolver: HostResolver = never_resolve, **kwargs: bool | float) -> str:
    options: dict[str, bool | float] = {"allow_http": False, "allow_private": False, **kwargs}
    with pytest.raises(UnsafeDestinationError) as caught:
        await check_destination_url(
            url,
            allow_http=bool(options["allow_http"]),
            allow_private=bool(options["allow_private"]),
            resolver=resolver,
            timeout_s=float(options.get("timeout_s", 2.0)),
        )
    assert str(caught.value) == f"destination rejected: {caught.value.reason}"
    return caught.value.reason


@pytest.mark.parametrize("address", BLOCKED)
def test_blocked_ranges(address: str) -> None:
    assert blocked_address(ip(address)) is True


@pytest.mark.parametrize("address", ALLOWED)
def test_public_addresses_allowed(address: str) -> None:
    assert blocked_address(ip(address)) is False
    assert blocked_address(ip(address), allow_private=True) is False


@pytest.mark.parametrize(
    "address", ["10.0.0.1", "127.0.0.1", "::1", "fd12::1", "192.168.1.1", "::ffff:10.0.0.1"]
)
def test_allow_private_relaxes_private_ranges(address: str) -> None:
    assert blocked_address(ip(address), allow_private=True) is False


@pytest.mark.parametrize(
    "address",
    ["169.254.169.254", "fd00:ec2::254", "::ffff:169.254.169.254", "224.0.0.1", "0.0.0.0", "::", "240.0.0.1"],  # noqa: S104
)
def test_metadata_and_special_addresses_blocked_even_when_private_allowed(address: str) -> None:
    assert blocked_address(ip(address), allow_private=True) is True


def test_metadata_addresses() -> None:
    assert {ip("169.254.169.254"), ip("fd00:ec2::254")} == METADATA_ADDRESSES


def test_parse_ip() -> None:
    assert parse_ip("[::1]") == ip("::1")
    assert parse_ip("10.0.0.1") == ip("10.0.0.1")
    assert parse_ip("hooks.example.com") is None


async def test_public_https_returns_every_address() -> None:
    resolver = resolver_of({"hooks.example.com": ["8.8.8.8", "2606:4700:4700::1111"]})
    addresses = await check_destination_url(
        "https://hooks.example.com/x", allow_http=False, allow_private=False, resolver=resolver
    )
    assert addresses == (ip("8.8.8.8"), ip("2606:4700:4700::1111"))


async def test_uppercase_scheme_accepted() -> None:
    resolver = resolver_of({"hooks.example.com": ["8.8.8.8"]})
    assert await check_destination_url(
        "HTTPS://hooks.example.com", allow_http=False, allow_private=False, resolver=resolver
    ) == (ip("8.8.8.8"),)


async def test_http_needs_allow_http() -> None:
    assert await reject_reason("http://hooks.example.com/x") == "scheme"
    resolver = resolver_of({"hooks.example.com": ["8.8.8.8"]})
    assert await check_destination_url(
        "http://hooks.example.com/x", allow_http=True, allow_private=False, resolver=resolver
    ) == (ip("8.8.8.8"),)


@pytest.mark.parametrize(
    "url", ["ftp://hooks.example.com/x", "file:///etc/passwd", "hooks.example.com/x", ""]
)
async def test_other_schemes_rejected(url: str) -> None:
    assert await reject_reason(url, allow_http=True) == "scheme"


@pytest.mark.parametrize("url", ["https:///x", "https://", "https://[::1"])
async def test_missing_or_malformed_host(url: str) -> None:
    assert await reject_reason(url) == "host"


@pytest.mark.parametrize("url", ["https://10.0.0.1/x", "https://[::1]:8443/x", "https://[::ffff:127.0.0.1]/"])
async def test_private_ip_literal_rejected_without_dns(url: str) -> None:
    assert await reject_reason(url) == "address"


async def test_ip_literal_allowed_without_dns() -> None:
    assert await check_destination_url(
        "https://[2606:4700:4700::1111]/x", allow_http=False, allow_private=False, resolver=never_resolve
    ) == (ip("2606:4700:4700::1111"),)


async def test_mixed_answer_rejected() -> None:
    resolver = resolver_of({"mixed.example.com": ["8.8.8.8", "10.0.0.1"]})
    assert await reject_reason("https://mixed.example.com", resolver) == "address"


@pytest.mark.parametrize("answers", [[], ["not-an-address"], ["8.8.8.8", "bogus"]])
async def test_unusable_answer_is_resolution_failure(answers: list[str]) -> None:
    resolver = resolver_of({"empty.example.com": answers})
    assert await reject_reason("https://empty.example.com", resolver) == "resolution"


async def test_resolver_error_is_resolution_failure() -> None:
    assert await reject_reason("https://gone.example.com", failing_resolver) == "resolution"


async def test_resolver_timeout_is_resolution_failure() -> None:
    assert await reject_reason("https://slow.example.com", hanging_resolver, timeout_s=0.01) == "resolution"


async def test_allow_private_lets_local_simulators_through() -> None:
    resolver = resolver_of({"localhost": ["127.0.0.1", "::1"]})
    addresses = await check_destination_url(
        "http://localhost:9100/hook", allow_http=True, allow_private=True, resolver=resolver
    )
    assert addresses == (ip("127.0.0.1"), ip("::1"))
    assert await reject_reason("http://localhost:9100/hook", resolver, allow_http=True) == "address"


async def test_metadata_host_rejected_with_private_allowed() -> None:
    assert (
        await reject_reason("http://169.254.169.254/latest", allow_http=True, allow_private=True) == "address"
    )


async def test_system_resolver_resolves_ip_literal_offline() -> None:
    assert await system_resolver("127.0.0.1") == ["127.0.0.1"]
