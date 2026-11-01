"""Send Call validation step 9 (specs/05-calls-api.md §4.1, Decision P05-T04a).

Order, first failure wins: high-cost prefix blocklist and do-not-call lists (`NUMBER_BLOCKED`, one message for
every list so the caller never learns which one matched), international policy (`INTERNATIONAL_NOT_ENABLED`),
then calling hours when the org enforces them (`OUTSIDE_CALLING_HOURS`). Scheduled calls are checked against
their scheduled instant at creation, and the dispatcher re-runs the whole step with the dispatch time.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from va_common.errors import AppError
from va_compliance.calling_hours import calling_hours_open
from va_compliance.dnc import find_block
from va_compliance.fraud import blocked_prefix
from va_compliance.international import international_allowed
from va_domain.org import OrgSettings

__all__ = ["NUMBER_BLOCKED_MESSAGE", "OUTSIDE_CALLING_HOURS_MESSAGE", "OrgFacts", "check_call_compliance"]

NUMBER_BLOCKED_MESSAGE: Final = "This number is blocked."
OUTSIDE_CALLING_HOURS_MESSAGE: Final = "The destination's local time is outside the allowed calling hours."
_ENTERPRISE_PLAN: Final = "enterprise"


@dataclass(frozen=True, slots=True)
class OrgFacts:
    """The calling org's row as the compliance step needs it (`limits` is the raw `orgs.limits` JSON)."""

    org_id: uuid.UUID
    plan_id: str
    kyc_level: int
    limits: Mapping[str, object]
    settings: OrgSettings


def _allowed_high_risk(limits: Mapping[str, object]) -> tuple[str, ...]:
    raw = limits.get("allowed_high_risk_prefixes")
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return tuple(str(item) for item in raw)
    return ()


async def check_call_compliance(  # noqa: PLR0913 - keyword-only inputs of one validation step
    session: AsyncSession,
    facts: OrgFacts,
    *,
    destination: str,
    region: str | None,
    at: datetime,
    now: datetime,
    scheduled: bool,
    allowed_countries: frozenset[str],
) -> None:
    """Raise the first compliance `AppError` for calling `destination` at `at`; return when it may proceed.

    `session` is the caller's tenant session of `facts.org_id`; `now` decides DNC expiry, `at` (the scheduled
    instant, or `now`) the calling hours. `region` is the destination's ISO region (`None` when unknown).
    """
    blocked = blocked_prefix(
        destination,
        org_blocked=facts.settings.blocked_prefixes,
        allowed_high_risk=_allowed_high_risk(facts.limits),
        enterprise=facts.plan_id == _ENTERPRISE_PLAN,
    )
    if blocked is not None or await find_block(session, facts.org_id, destination, now=now) is not None:
        raise AppError("NUMBER_BLOCKED", NUMBER_BLOCKED_MESSAGE, field="phone_number")
    if not international_allowed(
        region,
        settings=facts.settings,
        limits=facts.limits,
        kyc_level=facts.kyc_level,
        plan_id=facts.plan_id,
        allowed_countries=allowed_countries,
    ):
        label = region if region is not None else "this destination"
        raise AppError(
            "INTERNATIONAL_NOT_ENABLED",
            f"International calls to {label} are not enabled for this organization.",
            field="phone_number",
        )
    if facts.settings.compliance.enforce_calling_hours and not calling_hours_open(
        at, destination, settings=facts.settings
    ):
        raise AppError(
            "OUTSIDE_CALLING_HOURS",
            OUTSIDE_CALLING_HOURS_MESSAGE,
            field="start_time" if scheduled else "phone_number",
        )
