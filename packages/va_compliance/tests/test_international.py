"""International destination policy (specs/24 §6 item 7, specs/28 §9.1, specs/05 §4.1 Decision P05-T04a)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from va_compliance.international import DOMESTIC_NANP, international_allowed, parse_allowed_countries
from va_domain.org import load_settings

PLATFORM = parse_allowed_countries("US,CA,GB,AU,IE,NZ,DE,FR,ES,IT,NL,BE,SE,NO,DK,FI,MX,BR,IN,PH,SG")
OPEN_GB: dict[str, Any] = {"international_enabled": True, "allowed_countries": ["US", "CA", "GB"]}


def allowed(  # noqa: PLR0913 - keyword-only test knobs
    region: str | None,
    *,
    settings: Mapping[str, Any] | None = None,
    limits: Mapping[str, object] | None = None,
    kyc: int = 0,
    plan: str = "start",
    platform: frozenset[str] = PLATFORM,
) -> bool:
    return international_allowed(
        region,
        settings=load_settings(settings or {}),
        limits=limits or {},
        kyc_level=kyc,
        plan_id=plan,
        allowed_countries=platform,
    )


def test_parse_allowed_countries() -> None:
    assert parse_allowed_countries(" us, ca ,,") == frozenset({"US", "CA"})
    assert parse_allowed_countries("") == frozenset()
    assert {"US", "CA"} == DOMESTIC_NANP


@pytest.mark.parametrize("region", ["US", "CA"])
def test_us_and_canada_are_domestic(region: str) -> None:
    assert allowed(region)


def test_international_is_off_by_default() -> None:
    assert not allowed("GB", kyc=3, plan="scale")


def test_home_country_is_domestic_only_while_the_platform_allows_it() -> None:
    assert allowed("GB", settings={"default_country": "GB"})
    assert not allowed("GB", settings={"default_country": "GB"}, platform=frozenset({"US", "CA"}))


def test_platform_list_blocks_even_domestic_regions() -> None:
    assert not allowed("CA", platform=frozenset({"US"}))


@pytest.mark.parametrize(
    ("kyc", "plan", "limits", "expected"),
    [
        (2, "start", {}, True),
        (1, "start", {}, False),
        (1, "build", {}, True),
        (0, "build", {}, False),
        (0, "build", {"international_enabled": True}, True),
        (0, "build", {"international_enabled": "true"}, False),
        (0, "start", {"international_enabled": False}, False),
    ],
)
def test_plan_and_kyc_gate(kyc: int, plan: str, limits: dict[str, object], expected: bool) -> None:
    assert allowed("GB", settings=OPEN_GB, limits=limits, kyc=kyc, plan=plan) is expected


def test_region_must_be_in_the_org_list() -> None:
    assert not allowed("DE", settings=OPEN_GB, kyc=2)


def test_region_outside_the_platform_list_is_blocked_even_when_the_org_lists_it() -> None:
    settings = {"international_enabled": True, "allowed_countries": ["JP"]}
    assert not allowed("JP", settings=settings, kyc=3, plan="enterprise")


@pytest.mark.parametrize("region", [None, "001"])
def test_non_geographic_destinations_are_blocked(region: str | None) -> None:
    assert not allowed(region, settings=OPEN_GB, kyc=3)
