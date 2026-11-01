"""Destination parsing and caller-ID selection for Send Call (specs/05-calls-api.md §4.1 step 8; specs/16 §6.2
Decision P05-T03c).

Selection is read-only: rotation bookkeeping (`last_used_at`, daily counters, Redis round robin) and the
platform shared pool arrive with P10. Until then a call without `from`, `default_from_number`, or a
dialing strategy uses the org's own numbers ordered by `local_order`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import phonenumbers
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from va_common.errors import AppError
from va_common.nanp import npa, same_state
from va_db.models import NumberPoolMember, PhoneNumber
from va_domain.call_resolution import MergedParameters
from va_domain.org import OrgSettings
from va_domain.phone import InvalidPhoneNumberError, normalize_e164

__all__ = ["CallerId", "Candidate", "Destination", "local_order", "parse_destination", "select_caller_id"]

_PHONE_INVALID: Final = "phone_number is not a valid, dialable phone number."
_FROM_INVALID: Final = "`from` must be an active voice-capable number of this organization."
_FROM_REQUIRED: Final = "`from` is required with `encrypted_key`."
_DEFAULT_INVALID: Final = "The org default_from_number is not an active voice number."
_NONE_AVAILABLE: Final = (
    "No caller ID available: pass `from`, set default_from_number, or add a phone number."
)


@dataclass(frozen=True, slots=True)
class Candidate:
    """A caller number the call may use."""

    phone_number_id: uuid.UUID
    e164: str


@dataclass(frozen=True, slots=True)
class Destination:
    """The destination in E.164 and its ISO region (`None` for non-geographic numbers)."""

    e164: str
    region: str | None


@dataclass(frozen=True, slots=True)
class CallerId:
    destination: Destination
    caller: Candidate


def parse_destination(phone_number: str, country: str) -> Destination:
    """Normalize the request `phone_number` with the merged `country` hint (`INVALID_PHONE_NUMBER`)."""
    try:
        e164 = normalize_e164(phone_number, country)
    except InvalidPhoneNumberError as exc:
        raise AppError("INVALID_PHONE_NUMBER", _PHONE_INVALID, field="phone_number") from exc
    region = phonenumbers.region_code_for_number(phonenumbers.parse(e164))
    return Destination(e164=e164, region=region if region and region != "001" else None)


def local_order(candidates: Sequence[Candidate], destination: str) -> list[Candidate]:
    """Exact area-code matches first, then numbers in the destination's state/province, then the rest; ties
    by E.164 ascending. A non-NANP destination orders by E.164 only."""
    area_code = npa(destination)

    def rank(candidate: Candidate) -> tuple[int, str]:
        if area_code is not None and npa(candidate.e164) == area_code:
            return 0, candidate.e164
        if same_state(candidate.e164, destination):
            return 1, candidate.e164
        return 2, candidate.e164

    return sorted(candidates, key=rank)


def _candidates(org_id: uuid.UUID, account_id: uuid.UUID | None) -> Select[uuid.UUID, str]:
    """Active, voice-capable, not deleted numbers of the org on the platform account or the BYO account."""
    account = (
        PhoneNumber.telnyx_account_id.is_(None)
        if account_id is None
        else PhoneNumber.telnyx_account_id == account_id
    )
    return select(PhoneNumber.id, PhoneNumber.e164).where(
        PhoneNumber.org_id == org_id,
        PhoneNumber.deleted_at.is_(None),
        PhoneNumber.status == "active",
        PhoneNumber.capabilities.contains(["voice"]),
        account,
    )


async def _rows(session: AsyncSession, statement: Select[uuid.UUID, str]) -> list[Candidate]:
    return [Candidate(row_id, e164) for row_id, e164 in (await session.execute(statement)).all()]


async def _number(
    session: AsyncSession, org_id: uuid.UUID, account_id: uuid.UUID | None, e164: str
) -> Candidate | None:
    found = await _rows(session, _candidates(org_id, account_id).where(PhoneNumber.e164 == e164))
    return found[0] if found else None


async def _pool_member(session: AsyncSession, org_id: uuid.UUID, pool_id: uuid.UUID) -> Candidate | None:
    statement = (
        _candidates(org_id, None)
        .join(NumberPoolMember, NumberPoolMember.phone_number_id == PhoneNumber.id)
        .where(NumberPoolMember.pool_id == pool_id, NumberPoolMember.org_id == org_id)
        .order_by(NumberPoolMember.last_used_at.asc().nulls_first(), NumberPoolMember.phone_number_id)
        .limit(1)
    )
    found = await _rows(session, statement)
    return found[0] if found else None


async def _default(session: AsyncSession, org_id: uuid.UUID, org: OrgSettings, destination: str) -> Candidate:
    if org.default_from_number is not None:
        chosen = await _number(session, org_id, None, org.default_from_number)
        if chosen is None:
            raise AppError("INVALID_INPUT", _DEFAULT_INVALID, field="default_from_number")
        return chosen
    ordered = local_order(await _rows(session, _candidates(org_id, None)), destination)
    if not ordered:
        raise AppError("INVALID_INPUT", _NONE_AVAILABLE, field="from")
    return ordered[0]


async def _strategy(
    session: AsyncSession, org_id: uuid.UUID, merged: MergedParameters, destination: str
) -> Candidate | None:
    strategy = merged.dialing_strategy
    if strategy.type == "local":
        ordered = local_order(await _rows(session, _candidates(org_id, None)), destination)
        return ordered[0] if ordered else None
    if strategy.pool_id is not None:  # custom_pooling (the model requires pool_id exactly then)
        return await _pool_member(session, org_id, strategy.pool_id)
    return None


async def select_caller_id(  # noqa: PLR0913 - keyword-only selection inputs
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    phone_number: str,
    merged: MergedParameters,
    org: OrgSettings,
    account_id: uuid.UUID | None,
) -> CallerId:
    """§4.1 step 8: parse the destination, then pick the caller number.

    Explicit `from` (required with a BYO account, which only dials its own numbers) → `local` strategy →
    `custom_pooling` (least recently used member) → `default_from_number` → the org's own numbers.
    """
    destination = parse_destination(phone_number, merged.country)
    explicit = merged.from_
    if explicit is not None:
        chosen = await _number(session, org_id, account_id, explicit)
        if chosen is None:
            raise AppError("INVALID_INPUT", _FROM_INVALID, field="from")
        return CallerId(destination, chosen)
    if account_id is not None:
        raise AppError("INVALID_INPUT", _FROM_REQUIRED, field="from")
    chosen = await _strategy(session, org_id, merged, destination.e164)
    if chosen is None:
        chosen = await _default(session, org_id, org, destination.e164)
    return CallerId(destination, chosen)
