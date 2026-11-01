"""Send Call step 11: credit holds (specs/24 §5, Decision P05-T04b under specs/24 §6).

A hold reserves credit for an in-flight resource without touching ``orgs.credits_balance_micros``; it only
reduces the available balance (balance - held + credit limit). :func:`reserve` locks the org row
``FOR UPDATE`` so two concurrent reservations cannot both spend the last credit. Orgs whose credit limit is at
least $1 000 skip call holds (the meaning of ``settings.billing.skip_holds`` in specs/24 §5; no setting
exists). Every function runs in the caller's ``tenant_session`` of the org.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from va_billing.plans import EffectivePricing
from va_common.errors import AppError
from va_db.models import CreditHold, CreditHoldStatus, Org
from va_db.repositories import OrgRepo
from va_db.repositories.credits import held_micros
from va_db.session import use_org

__all__ = [
    "CALL_HOLD_CAP_MINUTES",
    "CALL_REFERENCE",
    "HOLD_GRACE",
    "INSUFFICIENT_CREDITS_MESSAGE",
    "SKIP_HOLDS_CREDIT_LIMIT_MICROS",
    "available_balance",
    "call_hold_amount",
    "capture",
    "holds_skipped",
    "release",
    "reserve",
    "reserve_call_hold",
]

CALL_HOLD_CAP_MINUTES: Final = 10
SKIP_HOLDS_CREDIT_LIMIT_MICROS: Final = 1_000_000_000
HOLD_GRACE: Final = timedelta(hours=2)
CALL_REFERENCE: Final = "call"
INSUFFICIENT_CREDITS_MESSAGE: Final = "Insufficient credits to place this call."


def call_hold_amount(max_duration_min: int, pricing: EffectivePricing) -> int:
    """``min(max_duration, 10) * connected rate + min attempt`` in micro-dollars."""
    minutes = min(max_duration_min, CALL_HOLD_CAP_MINUTES)
    return minutes * pricing.connected_rate_micros_per_min + pricing.min_attempt_micros


def holds_skipped(credit_limit_micros: int) -> bool:
    """Whether an org with this credit limit skips call holds."""
    return credit_limit_micros >= SKIP_HOLDS_CREDIT_LIMIT_MICROS


async def _locked_org(session: AsyncSession, org_id: uuid.UUID) -> Org:
    org = await OrgRepo(session).lock(org_id)
    if org is None:
        raise AppError("NOT_FOUND", "Organization not found.")
    return org


async def _available(session: AsyncSession, org: Org) -> int:
    return org.credits_balance_micros - await held_micros(session, org.id) + org.credit_limit_micros


async def available_balance(session: AsyncSession, org_id: uuid.UUID) -> int:
    """Balance minus open holds plus the credit limit (no lock; for display)."""
    org = await session.get(Org, org_id)
    if org is None:
        raise AppError("NOT_FOUND", "Organization not found.")
    return await _available(session, org)


async def reserve(  # noqa: PLR0913 - one hold row's attributes
    session: AsyncSession,
    org_id: uuid.UUID,
    amount_micros: int,
    *,
    reference_type: str,
    reference_id: str,
    expires_at: datetime,
    message: str = INSUFFICIENT_CREDITS_MESSAGE,
) -> CreditHold:
    """Lock the org, check the available balance, and insert a ``held`` hold of ``amount_micros``.

    ``INSUFFICIENT_CREDITS`` (with ``message``) when the available balance is below the amount.
    """
    if amount_micros < 0:
        msg = f"a hold amount cannot be negative: {amount_micros}"
        raise ValueError(msg)
    org = await _locked_org(session, org_id)
    if await _available(session, org) < amount_micros:
        raise AppError("INSUFFICIENT_CREDITS", message)
    hold = CreditHold(
        org_id=org_id,
        reference_type=reference_type,
        reference_id=reference_id,
        amount_micros=amount_micros,
        status="held",
        expires_at=expires_at,
    )
    async with use_org(session, org_id):
        session.add(hold)
        await session.flush()
    return hold


async def reserve_call_hold(  # noqa: PLR0913 - keyword-only call facts
    session: AsyncSession,
    org_id: uuid.UUID,
    call_id: uuid.UUID,
    *,
    credit_limit_micros: int,
    max_duration_min: int,
    pricing: EffectivePricing,
    now: datetime,
) -> CreditHold | None:
    """Reserve a call's hold, expiring ``max_duration + 2 h`` after ``now``; ``None`` when skipped."""
    if holds_skipped(credit_limit_micros):
        return None
    return await reserve(
        session,
        org_id,
        call_hold_amount(max_duration_min, pricing),
        reference_type=CALL_REFERENCE,
        reference_id=str(call_id),
        expires_at=now + timedelta(minutes=max_duration_min) + HOLD_GRACE,
    )


async def _settle(
    session: AsyncSession, org_id: uuid.UUID, hold_id: uuid.UUID, status: CreditHoldStatus
) -> bool:
    result = await session.execute(
        update(CreditHold)
        .where(CreditHold.org_id == org_id, CreditHold.id == hold_id, CreditHold.status == "held")
        .values(status=status)
        .returning(CreditHold.id)
        .execution_options(synchronize_session="fetch")
    )
    return result.scalar_one_or_none() is not None


async def capture(session: AsyncSession, org_id: uuid.UUID, hold_id: uuid.UUID) -> bool:
    """Mark a ``held`` hold captured; ``False`` when already settled or not one of ``org_id``'s holds."""
    return await _settle(session, org_id, hold_id, "captured")


async def release(session: AsyncSession, org_id: uuid.UUID, hold_id: uuid.UUID) -> bool:
    """Mark a ``held`` hold released; ``False`` when already settled or not one of ``org_id``'s holds."""
    return await _settle(session, org_id, hold_id, "released")
