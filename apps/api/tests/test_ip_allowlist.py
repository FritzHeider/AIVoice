"""Matching client addresses against an org ``ip_allowlist`` (specs/04-auth-tenancy-rbac.md §7 Decision
"Enforcing ip_allowlist and session_max_age_days (P02-T12c)")."""

from __future__ import annotations

import pytest

from va_api.auth.ip_allowlist import ip_allowed

V4 = ["203.0.113.0/24"]
V6 = ["2001:db8::/32"]


@pytest.mark.parametrize("ip", ["203.0.113.7", "198.51.100.9", "2001:db8::1", None, "testclient"])
def test_an_empty_list_allows_every_request(ip: str | None) -> None:
    assert ip_allowed(ip, [])


@pytest.mark.parametrize(
    ("ip", "networks", "expected"),
    [
        ("203.0.113.7", V4, True),
        ("203.0.113.255", V4, True),
        ("203.0.114.1", V4, False),
        ("198.51.100.9", ["198.51.100.9/32"], True),
        ("198.51.100.10", ["198.51.100.9/32"], False),
        ("2001:db8:1::5", V6, True),
        ("2001:db9::5", V6, False),
        ("::ffff:203.0.113.7", V4, True),
        ("::ffff:198.51.100.9", V4, False),
        ("203.0.113.7", V6, False),
        ("2001:db8::1", V4, False),
        ("10.9.8.7", [*V6, "10.0.0.0/8"], True),
    ],
)
def test_addresses_match_their_networks(ip: str, networks: list[str], expected: bool) -> None:
    assert ip_allowed(ip, networks) is expected


@pytest.mark.parametrize("ip", [None, "testclient", "", "203.0.113"])
def test_a_missing_or_malformed_address_never_matches_a_list(ip: str | None) -> None:
    assert not ip_allowed(ip, V4)
