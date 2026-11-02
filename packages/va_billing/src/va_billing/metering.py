"""Call metering: ``finalize_call`` posts a call's usage, charges the ledger, and settles its hold.

Implements specs/24 §3 (call meters, rounding), §4 steps 1-3, and §5 capture/release for the three call
meters; the scope and the deferred parts (COGS, STT/TTS meters, the ClickHouse mirror, auto-recharge, low
balance, ``bill_to_parent``) are listed in the specs/24 §4 Decision P05-T18a.

:func:`plan_call_usage` is pure: it turns the call's facts into the usage rows to post. :func:`finalize_call`
runs in the caller's ``tenant_session(org_id)``. It locks the org row first, so concurrent finalizes of the
same org serialise, and every write is idempotent: usage rows are inserted ``ON CONFLICT DO NOTHING`` on
``(org_id, idempotency_key)``, only newly inserted rows debit the balance, and ``calls.price_micros`` is
recomputed from every call meter row of the call.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Final, Literal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from va_billing.holds import CALL_REFERENCE, capture, release
from va_billing.plans import EffectivePricing, effective_pricing, int_override
from va_common.errors import AppError
from va_common.ids import new_id
from va_common.logging import get_logger
from va_db.call_states import is_terminal
from va_db.models import Call, CallDirection, CallStatus, CreditLedger, Plan, UsageEvent, UsageMeter
from va_db.repositories import CallLegRepo, OrgRepo

__all__ = [
    "CALL_METERS",
    "SECONDS_PER_MINUTE",
    "CallNotEndedError",
    "CallUsageFacts",
    "FinalizedCall",
    "HoldOutcome",
    "PlannedUsage",
    "call_idempotency_key",
    "finalize_call",
    "plan_call_usage",
    "pricing_from_snapshot",
    "round_half_up",
    "seconds_amount",
]

CALL_METERS: Final[tuple[UsageMeter, ...]] = (
    "call_connected_seconds",
    "call_transfer_seconds",
    "call_attempt",
)
SECONDS_PER_MINUTE: Final = 60
PRICING_KEY: Final = "pricing"
CALL_NOT_FOUND_MESSAGE: Final = "Call not found."
ORG_NOT_FOUND_MESSAGE: Final = "Organization not found."
PLAN_MISSING_MESSAGE: Final = "The organization's plan is not available."

_KEY_SUFFIXES: Final[Mapping[UsageMeter, str]] = {
    "call_connected_seconds": "connected",
    "call_transfer_seconds": "transfer",
    "call_attempt": "attempt",
}

HoldOutcome = Literal["captured", "released", "none", "settled"]

_log = get_logger(__name__)


def round_half_up(value: Decimal) -> int:
    """``value`` rounded to the nearest integer, halves away from zero (specs/24 §3 rounding)."""
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def seconds_amount(seconds: Decimal, rate_micros_per_min: int) -> int:
    """The charge of ``seconds`` at a per-minute rate, ``round_half_up(seconds * rate / 60)`` µ$."""
    return round_half_up(seconds * rate_micros_per_min / SECONDS_PER_MINUTE)


def call_idempotency_key(call_id: uuid.UUID, meter: UsageMeter) -> str:
    """``call:{id}:connected|transfer|attempt``, the specs/24 §3 key of a call meter."""
    return f"call:{call_id}:{_KEY_SUFFIXES[meter]}"


@dataclass(frozen=True, slots=True)
class CallUsageFacts:
    """What a call's price depends on.

    ``dialed`` is true when the call has a primary leg (it reached the carrier); ``byo`` when that leg was
    placed on the org's own Telnyx account.
    """

    call_id: uuid.UUID
    direction: CallDirection
    dialed: bool
    connected_seconds: Decimal
    transfer_seconds: Decimal
    byo: bool
    pricing: EffectivePricing


@dataclass(frozen=True, slots=True)
class PlannedUsage:
    """One ``usage_events`` row to post."""

    meter: UsageMeter
    quantity: Decimal
    unit_price_micros: int
    amount_micros: int
    idempotency_key: str


def _seconds_usage(
    call_id: uuid.UUID, meter: UsageMeter, seconds: Decimal, rate_micros_per_min: int
) -> PlannedUsage:
    return PlannedUsage(
        meter=meter,
        quantity=seconds,
        unit_price_micros=round_half_up(Decimal(rate_micros_per_min) / SECONDS_PER_MINUTE),
        amount_micros=seconds_amount(seconds, rate_micros_per_min),
        idempotency_key=call_idempotency_key(call_id, meter),
    )


def plan_call_usage(facts: CallUsageFacts) -> list[PlannedUsage]:
    """The call meter rows of a finished call, in the order connected, transfer, attempt.

    A meter with zero seconds is not posted; the attempt row of a dialed outbound call always is (its amount
    is the minimum attempt price minus the connected charge, floored at 0). A call that never reached the
    carrier posts nothing.
    """
    planned: list[PlannedUsage] = []
    pricing = facts.pricing
    connected_amount = 0
    if facts.connected_seconds > 0:
        connected = _seconds_usage(
            facts.call_id,
            "call_connected_seconds",
            facts.connected_seconds,
            pricing.connected_rate_micros_per_min,
        )
        connected_amount = connected.amount_micros
        planned.append(connected)
    if facts.transfer_seconds > 0:
        rate = 0 if facts.byo else pricing.transfer_rate_micros_per_min
        planned.append(_seconds_usage(facts.call_id, "call_transfer_seconds", facts.transfer_seconds, rate))
    if facts.direction == "outbound" and facts.dialed:
        planned.append(
            PlannedUsage(
                meter="call_attempt",
                quantity=Decimal(1),
                unit_price_micros=pricing.min_attempt_micros,
                amount_micros=max(0, pricing.min_attempt_micros - connected_amount),
                idempotency_key=call_idempotency_key(facts.call_id, "call_attempt"),
            )
        )
    return planned


def pricing_from_snapshot(snapshot: object) -> EffectivePricing | None:
    """The ``calls.config.pricing`` snapshot as pricing; ``None`` unless every rate is an integer."""
    if not isinstance(snapshot, Mapping):
        return None
    rates: dict[str, int] = {}
    for field in fields(EffectivePricing):
        value = int_override(snapshot.get(field.name))
        if value is None:
            return None
        rates[field.name] = value
    return EffectivePricing(**rates)


class CallNotEndedError(Exception):
    """The call has not reached a terminal status yet, so it cannot be priced."""

    def __init__(self, status: CallStatus) -> None:
        super().__init__(f"call is still {status}")
        self.status = status


@dataclass(frozen=True, slots=True)
class FinalizedCall:
    """The outcome of :func:`finalize_call`.

    ``posted`` holds the idempotency keys this run inserted (empty on a re-run); ``hold`` is ``settled`` when
    the hold was already captured or released.
    """

    price_micros: int
    posted: tuple[str, ...]
    hold: HoldOutcome
    balance_after_micros: int


async def _call_pricing(
    session: AsyncSession, call: Call, plan_id: str, limits: Mapping[str, object]
) -> EffectivePricing:
    snapshot = pricing_from_snapshot(call.config.get(PRICING_KEY))
    if snapshot is not None:
        return snapshot
    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise AppError("SERVICE_UNAVAILABLE", PLAN_MISSING_MESSAGE)
    _log.warning("billing.pricing_snapshot_missing", call_id=str(call.id), plan_id=plan_id)
    return effective_pricing(plan, limits)


async def _insert_usage(
    session: AsyncSession, call: Call, planned: list[PlannedUsage], occurred_at: datetime
) -> list[tuple[str, UsageMeter, int]]:
    """Insert the planned rows that are not posted yet; ``(key, meter, amount)`` of each inserted row."""
    if not planned:
        return []
    rows = [
        {
            "id": new_id(),
            "org_id": call.org_id,
            "meter": usage.meter,
            "quantity": usage.quantity,
            "unit_price_micros": usage.unit_price_micros,
            "amount_micros": usage.amount_micros,
            "reference_type": CALL_REFERENCE,
            "reference_id": str(call.id),
            "idempotency_key": usage.idempotency_key,
            "occurred_at": occurred_at,
        }
        for usage in planned
    ]
    statement = (
        insert(UsageEvent)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["org_id", "idempotency_key"])
        .returning(UsageEvent.idempotency_key)
    )
    inserted = set((await session.scalars(statement)).all())
    return [(u.idempotency_key, u.meter, u.amount_micros) for u in planned if u.idempotency_key in inserted]


async def _call_price(session: AsyncSession, org_id: uuid.UUID, call_id: uuid.UUID) -> int:
    total = await session.scalar(
        select(func.coalesce(func.sum(UsageEvent.amount_micros), 0)).where(
            UsageEvent.org_id == org_id,
            UsageEvent.reference_type == CALL_REFERENCE,
            UsageEvent.reference_id == str(call_id),
            UsageEvent.meter.in_(CALL_METERS),
        )
    )
    return int(total or 0)


async def _settle_hold(session: AsyncSession, call: Call, price_micros: int) -> HoldOutcome:
    if call.credit_hold_id is None:
        return "none"
    if price_micros > 0:
        return "captured" if await capture(session, call.org_id, call.credit_hold_id) else "settled"
    return "released" if await release(session, call.org_id, call.credit_hold_id) else "settled"


async def finalize_call(
    session: AsyncSession, org_id: uuid.UUID, call_id: uuid.UUID, *, now: datetime
) -> FinalizedCall:
    """Price a terminal call in the caller's ``tenant_session(org_id)`` (specs/24 §4 steps 1-3).

    Posts the call meter rows, one ``usage`` ledger row per newly posted non-zero amount, debits the balance,
    sets ``calls.price_micros``, and captures the call's hold (price > 0) or releases it. ``NOT_FOUND``
    without the org, ``CALL_NOT_FOUND`` without the call, :class:`CallNotEndedError` for a non-terminal call.
    """
    org = await OrgRepo(session).lock(org_id)
    if org is None:
        raise AppError("NOT_FOUND", ORG_NOT_FOUND_MESSAGE)
    call = await session.scalar(
        select(Call)
        .where(Call.org_id == org_id, Call.id == call_id)
        .execution_options(populate_existing=True)
    )
    if call is None:
        raise AppError("CALL_NOT_FOUND", CALL_NOT_FOUND_MESSAGE)
    if not is_terminal(call.status):
        raise CallNotEndedError(call.status)
    leg = await CallLegRepo(session, org_id).primary(call_id)
    facts = CallUsageFacts(
        call_id=call_id,
        direction=call.direction,
        dialed=leg is not None,
        connected_seconds=call.connected_seconds,
        transfer_seconds=call.transfer_seconds,
        byo=leg is not None and leg.telnyx_account_id is not None,
        pricing=await _call_pricing(session, call, org.plan_id, org.limits),
    )
    inserted = await _insert_usage(session, call, plan_call_usage(facts), call.ended_at or now)
    for key, meter, amount in inserted:
        if amount <= 0:
            continue
        org.credits_balance_micros -= amount
        session.add(
            CreditLedger(
                id=new_id(),
                org_id=org_id,
                entry_type="usage",
                amount_micros=-amount,
                balance_after_micros=org.credits_balance_micros,
                reference_type=CALL_REFERENCE,
                reference_id=str(call_id),
                description=meter,
                idempotency_key=key,
            )
        )
    await session.flush()
    price = await _call_price(session, org_id, call_id)
    call.price_micros = price
    hold = await _settle_hold(session, call, price)
    await session.flush()
    posted = tuple(key for key, _, _ in inserted)
    _log.info(
        "billing.call_finalized", call_id=str(call_id), price_micros=price, posted=len(posted), hold=hold
    )
    return FinalizedCall(
        price_micros=price, posted=posted, hold=hold, balance_after_micros=org.credits_balance_micros
    )
