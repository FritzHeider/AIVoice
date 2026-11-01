"""High-cost prefix blocklist (specs/28 §9.2)."""

from __future__ import annotations

import re

import pytest

from va_compliance.fraud import ALWAYS_BLOCKED_PREFIXES, BLOCKED_PREFIXES, HIGH_RISK_PREFIXES, blocked_prefix

SPEC_HIGH_RISK = (
    "+882", "+883", "+870", "+881", "+979", "+808", "+247", "+290", "+500", "+674", "+675", "+677", "+678",
    "+680", "+682", "+683", "+686", "+688", "+690", "+691", "+692", "+239", "+245", "+252", "+37190",
)  # fmt: skip


def check(
    e164: str, *, org: tuple[str, ...] = (), allowed: tuple[str, ...] = (), enterprise: bool = False
) -> str | None:
    return blocked_prefix(e164, org_blocked=org, allowed_high_risk=allowed, enterprise=enterprise)


def test_lists_match_the_spec() -> None:
    assert ALWAYS_BLOCKED_PREFIXES == ("+1900", "+1976")
    assert HIGH_RISK_PREFIXES == SPEC_HIGH_RISK
    assert BLOCKED_PREFIXES == ALWAYS_BLOCKED_PREFIXES + SPEC_HIGH_RISK
    assert all(re.fullmatch(r"\+[0-9]+", prefix) for prefix in BLOCKED_PREFIXES)


@pytest.mark.parametrize("e164", ["+19005551234", "+19765551234"])
def test_premium_nanp_codes_are_always_blocked(e164: str) -> None:
    assert check(e164) == e164[:5]
    assert check(e164, allowed=(e164[:5],), enterprise=True) == e164[:5]


def test_high_risk_prefix_is_blocked_unless_an_enterprise_org_allows_it() -> None:
    assert check("+88212345678") == "+882"
    assert check("+88212345678", allowed=("+882",)) == "+882"
    assert check("+88212345678", allowed=("+882",), enterprise=True) is None
    assert check("+88212345678", allowed=("+88",), enterprise=True) == "+882"


def test_latvia_premium_only_blocks_the_90_range() -> None:
    assert check("+37190123456") == "+37190"
    assert check("+37121234567") is None


def test_org_prefixes_add_to_the_list_and_are_never_unblocked() -> None:
    assert check("+442071234567", org=("+4420",)) == "+4420"
    assert check("+442071234567") is None
    assert check("+88212345678", org=("+882",), allowed=("+882",), enterprise=True) == "+882"


def test_longest_matching_prefix_is_returned() -> None:
    assert check("+88212345678", org=("+8821",)) == "+8821"
    assert check("+88212345678", org=("+88",)) == "+882"


@pytest.mark.parametrize("e164", ["+18095551234", "+14155551234", "+447911123456"])
def test_ordinary_destinations_pass(e164: str) -> None:
    assert check(e164) is None
