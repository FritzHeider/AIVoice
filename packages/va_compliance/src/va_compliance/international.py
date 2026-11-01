"""International destination policy (specs/24-billing-and-limits.md §6 item 7, specs/28 §9.1).

Rules (first failing wins; specs/05 §4.1 Decision P05-T04a):

1. A region outside the platform's `INTERNATIONAL_ALLOWED_COUNTRIES` is never dialable.
2. Domestic destinations (US, CA, or the org's `default_country`) are allowed.
3. Anything else needs `settings.international_enabled`, the region in `settings.allowed_countries`, and the
   plan/KYC gate: an `orgs.limits.international_enabled` override of `true`, or `kyc_level >= 2`, or
   `kyc_level >= 1` on a plan other than `start`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from va_domain.org import OrgSettings

__all__ = ["DOMESTIC_NANP", "international_allowed", "parse_allowed_countries"]

DOMESTIC_NANP: Final = frozenset({"US", "CA"})

_FULL_KYC: Final = 2
_BASIC_KYC: Final = 1
_ENTRY_PLAN: Final = "start"


def parse_allowed_countries(raw: str) -> frozenset[str]:
    """Parse a comma-separated region list (`" us, ca ,,"` -> `{"US", "CA"}`)."""
    return frozenset(part.strip().upper() for part in raw.split(",") if part.strip())


def _plan_allows(limits: Mapping[str, object], kyc_level: int, plan_id: str) -> bool:
    # Only a real boolean override counts; a string such as "true" in hand-edited JSON is ignored.
    if limits.get("international_enabled") is True:
        return True
    return kyc_level >= _FULL_KYC or (kyc_level >= _BASIC_KYC and plan_id != _ENTRY_PLAN)


def international_allowed(  # noqa: PLR0913 - keyword-only org facts
    region: str | None,
    *,
    settings: OrgSettings,
    limits: Mapping[str, object],
    kyc_level: int,
    plan_id: str,
    allowed_countries: frozenset[str],
) -> bool:
    """Whether an org may call a destination in `region` (`None`/`"001"` = non-geographic, never listed)."""
    if region is None or region not in allowed_countries:
        return False
    if region in DOMESTIC_NANP or region == settings.default_country:
        return True
    return (
        settings.international_enabled
        and region in settings.allowed_countries
        and _plan_allows(limits, kyc_level, plan_id)
    )
