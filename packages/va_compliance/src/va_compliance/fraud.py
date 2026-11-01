"""High-cost destination blocklist (specs/28-security-and-compliance.md §9.2).

Matching is longest-prefix on the E.164 string. `+1900` and `+1976` (NANP premium codes) are always blocked;
the high-risk destinations are blocked unless an enterprise org lists the exact prefix in
`orgs.limits.allowed_high_risk_prefixes`. An org's own `blocked_prefixes` setting adds to the list.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

__all__ = ["ALWAYS_BLOCKED_PREFIXES", "BLOCKED_PREFIXES", "HIGH_RISK_PREFIXES", "blocked_prefix"]

ALWAYS_BLOCKED_PREFIXES: Final[tuple[str, ...]] = ("+1900", "+1976")

HIGH_RISK_PREFIXES: Final[tuple[str, ...]] = (
    "+882",  # international networks
    "+883",  # international networks
    "+870",  # Inmarsat
    "+881",  # satellite
    "+979",  # international premium rate
    "+808",  # shared cost
    "+247",  # Ascension
    "+290",  # St Helena
    "+500",  # Falklands
    "+674",  # Nauru
    "+675",  # Papua New Guinea
    "+677",  # Solomon Islands
    "+678",  # Vanuatu
    "+680",  # Palau
    "+682",  # Cook Islands
    "+683",  # Niue
    "+686",  # Kiribati
    "+688",  # Tuvalu
    "+690",  # Tokelau
    "+691",  # Micronesia
    "+692",  # Marshall Islands
    "+239",  # Sao Tome
    "+245",  # Guinea-Bissau
    "+252",  # Somalia
    "+37190",  # Latvia premium ("+371 90" in the spec)
)

# The platform list as one tuple (the spec's `BLOCKED_PREFIXES`).
BLOCKED_PREFIXES: Final[tuple[str, ...]] = ALWAYS_BLOCKED_PREFIXES + HIGH_RISK_PREFIXES


def _longest(e164: str, prefixes: Sequence[str]) -> str | None:
    matches = [prefix for prefix in prefixes if e164.startswith(prefix)]
    return max(matches, key=len) if matches else None


def blocked_prefix(
    e164: str, *, org_blocked: Sequence[str], allowed_high_risk: Sequence[str], enterprise: bool
) -> str | None:
    """The longest blocking prefix that matches `e164`, or `None` when the destination may be dialled.

    `allowed_high_risk` unblocks high-risk prefixes (exact prefix match) only when `enterprise` is true; it
    never unblocks an always-blocked prefix or one of the org's own `org_blocked` prefixes.
    """
    unblocked = set(allowed_high_risk) if enterprise else set()
    candidates = [
        *ALWAYS_BLOCKED_PREFIXES,
        *(prefix for prefix in HIGH_RISK_PREFIXES if prefix not in unblocked),
        *org_blocked,
    ]
    return _longest(e164, candidates)
