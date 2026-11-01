"""Seed calls and legs on a migrated database (shared by the va_db and telephony-edge tests)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from tenant_rows import OrgFactory

from va_common.ids import new_id
from va_db.models import Call, CallLeg, CallStatus
from va_db.session import Database

__all__ = ["SeededCall", "load_call", "load_leg", "seed_call", "seed_leg"]


@dataclass(frozen=True, slots=True)
class SeededCall:
    org_id: uuid.UUID
    call_id: uuid.UUID


async def seed_call(
    database: Database, *, status: CallStatus = "dialing", org_id: uuid.UUID | None = None, **values: Any
) -> SeededCall:
    """An outbound task call in ``status`` (a fresh org unless ``org_id`` is given); ``values`` override the
    defaults (``config``, numbers, ...)."""
    org = org_id if org_id is not None else await OrgFactory(database).create()
    defaults: dict[str, Any] = {
        "direction": "outbound",
        "runner_type": "task",
        "config": {},
        "to_number": "+14155550100",
        "from_number": "+14155550199",
    }
    call = Call(id=new_id(), org_id=org, status=status, **{**defaults, **values})
    async with database.tenant_session(org) as session:
        session.add(call)
    return SeededCall(org_id=org, call_id=call.id)


async def seed_leg(database: Database, call: SeededCall, call_control_id: str, **values: Any) -> None:
    defaults: dict[str, Any] = {"role": "primary", "state": "initiated"}
    leg = CallLeg(
        id=new_id(),
        org_id=call.org_id,
        call_id=call.call_id,
        call_control_id=call_control_id,
        **{**defaults, **values},
    )
    async with database.tenant_session(call.org_id) as session:
        session.add(leg)


async def load_call(database: Database, call: SeededCall) -> Call:
    async with database.tenant_session(call.org_id) as session:
        return await session.get_one(Call, call.call_id)


async def load_leg(database: Database, call: SeededCall, call_control_id: str) -> CallLeg | None:
    async with database.tenant_session(call.org_id) as session:
        return await session.scalar(select(CallLeg).where(CallLeg.call_control_id == call_control_id))
