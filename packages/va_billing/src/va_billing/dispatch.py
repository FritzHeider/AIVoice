"""Send Call steps 9-11 in one place: compliance, limits, credit hold (specs/05 §4.1, Decision P05-T04b).

:func:`admit_call` runs ``va_compliance.check_call_compliance`` (nothing is counted when it fails), then
:meth:`CallLimiter.acquire`, then the credit hold; when the hold fails the counters taken are given back.
Scheduled calls run only the compliance step at creation; limits and the hold are taken when the dispatcher
places them (a hold reserved days ahead would expire first). :meth:`Admitted.compensate` undoes a successful
admission when a later step (insert, workflow start) fails.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from va_billing import holds
from va_billing.limits import CallLimiter, LimitInputs, LimitTicket
from va_billing.plans import EffectiveLimits, EffectivePricing, effective_limits, effective_pricing
from va_common.errors import AppError
from va_compliance.compliance import OrgFacts, check_call_compliance
from va_db.models import CreditHold
from va_db.repositories import OrgRepo
from va_domain.org import OrgSettings, load_settings

__all__ = [
    "PLAN_UNAVAILABLE_MESSAGE",
    "Admitted",
    "BillingOrg",
    "CallAdmission",
    "admit_call",
    "load_billing_org",
]

PLAN_UNAVAILABLE_MESSAGE = "Billing is not configured for this organization."


@dataclass(frozen=True, slots=True)
class BillingOrg:
    """The calling org as steps 9-11 need it: raw ``orgs.limits``, settings, effective limits and prices."""

    org_id: uuid.UUID
    plan_id: str
    kyc_level: int
    created_at: datetime
    credit_limit_micros: int
    limits_raw: Mapping[str, object]
    settings: OrgSettings
    limits: EffectiveLimits
    pricing: EffectivePricing

    def facts(self) -> OrgFacts:
        """The compliance step's view of the org."""
        return OrgFacts(
            org_id=self.org_id,
            plan_id=self.plan_id,
            kyc_level=self.kyc_level,
            limits=self.limits_raw,
            settings=self.settings,
        )


async def load_billing_org(session: AsyncSession, org_id: uuid.UUID) -> BillingOrg:
    """Read the org and its plan in the caller's session (``NOT_FOUND`` without the org).

    ``SERVICE_UNAVAILABLE`` when the org's ``plan_id`` names no plan (``orgs.plan_id`` has no foreign key).
    """
    overview = await OrgRepo(session).overview(org_id)
    if overview is None:
        raise AppError("NOT_FOUND", "Organization not found.")
    org, plan = overview.org, overview.plan
    if plan is None:
        raise AppError("SERVICE_UNAVAILABLE", PLAN_UNAVAILABLE_MESSAGE)
    return BillingOrg(
        org_id=org.id,
        plan_id=org.plan_id,
        kyc_level=org.kyc_level,
        created_at=org.created_at,
        credit_limit_micros=org.credit_limit_micros,
        limits_raw=dict(org.limits),
        settings=load_settings(org.settings),
        limits=effective_limits(plan, org.limits),
        pricing=effective_pricing(plan, org.limits),
    )


@dataclass(frozen=True, slots=True)
class CallAdmission:
    """One call to admit: ``at`` is the scheduled instant (or now), ``region`` the destination ISO region."""

    call_id: uuid.UUID
    destination: str
    region: str | None
    max_duration_min: int
    at: datetime
    scheduled: bool
    allowed_countries: frozenset[str]
    concurrency_share: float = 1.0


@dataclass(slots=True)
class Admitted:
    """What an admission took: the price snapshot, the hold (``None``: skipped or scheduled), the counters."""

    org_id: uuid.UUID
    call_id: uuid.UUID
    pricing: EffectivePricing
    hold: CreditHold | None
    ticket: LimitTicket | None

    async def compensate(self, session: AsyncSession, limiter: CallLimiter) -> None:
        """Give back the counters, the concurrency slot, and the hold (each part idempotent)."""
        if self.ticket is not None:
            await self.ticket.rollback()
            await limiter.release_call(self.org_id, self.call_id)
        if self.hold is not None:
            await holds.release(session, self.org_id, self.hold.id)


async def admit_call(
    session: AsyncSession, limiter: CallLimiter, org: BillingOrg, call: CallAdmission, *, now: datetime
) -> Admitted:
    """Run steps 9-11 for ``call`` in the caller's tenant session of ``org``; raise the first failure."""
    await check_call_compliance(
        session,
        org.facts(),
        destination=call.destination,
        region=call.region,
        at=call.at,
        now=now,
        scheduled=call.scheduled,
        allowed_countries=call.allowed_countries,
    )
    if call.scheduled:
        return Admitted(org_id=org.org_id, call_id=call.call_id, pricing=org.pricing, hold=None, ticket=None)
    ticket = await limiter.acquire(
        LimitInputs(
            org_id=org.org_id,
            org_created_at=org.created_at,
            limits=org.limits,
            settings=org.settings,
            destination=call.destination,
            region=call.region,
            call_id=call.call_id,
            max_duration_min=call.max_duration_min,
            concurrency_share=call.concurrency_share,
        )
    )
    try:
        hold = await holds.reserve_call_hold(
            session,
            org.org_id,
            call.call_id,
            credit_limit_micros=org.credit_limit_micros,
            max_duration_min=call.max_duration_min,
            pricing=org.pricing,
            now=now,
        )
    except BaseException:
        await ticket.rollback()
        raise
    return Admitted(org_id=org.org_id, call_id=call.call_id, pricing=org.pricing, hold=hold, ticket=ticket)
