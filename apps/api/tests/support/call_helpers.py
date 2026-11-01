"""Send Call endpoint test helpers: request builders and a callable org (fakes live in ``call_fakes``).

:func:`callable_org` seeds an org on the module database that can place a basic task call: plan ``build``,
created 30 days before the container clock (not a new account), $50 of credit, an org voice ``Karen``, an
owned caller ID, and a live ``*`` API key.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
from call_fakes import PUBLIC_ADDRESS, FakeCallStarter, FakeResolver
from call_input_rows import Seed, random_e164
from identity_db import IdentityDb
from session_helpers import SessionContainer, fund, set_org, store_api_key

__all__ = [
    "CALLS",
    "PUBLIC_ADDRESS",
    "CallableOrg",
    "FakeCallStarter",
    "FakeResolver",
    "bearer",
    "business_hours_today",
    "callable_org",
    "send_call",
    "task_body",
]

CALLS: Final = "/v1/calls"


def business_hours_today() -> datetime:
    """Today 18:00 UTC: inside the migration's ``call_events`` partitions and daytime in US time zones."""
    today = datetime.now(UTC).date()
    return datetime(today.year, today.month, today.day, 18, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class CallableOrg:
    seed: Seed
    key: str
    from_number: str
    from_number_id: uuid.UUID
    voice_id: uuid.UUID

    @property
    def org_id(self) -> uuid.UUID:
        return self.seed.org_id


async def callable_org(
    identity: IdentityDb, container: SessionContainer, *, dollars: str | None = "50", plan_id: str = "build"
) -> CallableOrg:
    seed = await Seed.create(identity.database)
    await set_org(
        identity, seed.org_id, plan_id=plan_id, created_at=container.clock.now() - timedelta(days=30)
    )
    if dollars is not None:
        await fund(container, seed.org_id, dollars)
    voice_id = await seed.voice("Karen")
    number_id, e164 = await seed.number()
    key, _ = await store_api_key(container, seed.org_id)
    return CallableOrg(seed=seed, key=key, from_number=e164, from_number_id=number_id, voice_id=voice_id)


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def task_body(org: CallableOrg, **overrides: Any) -> dict[str, Any]:
    """A minimal task call from the org's number to a fresh 415 number."""
    return {
        "phone_number": random_e164(),
        "from": org.from_number,
        "task": "You are Maya from Bella Pasta. Confirm tonight's reservation.",
        "voice": "Karen",
        **overrides,
    }


async def send_call(
    client: httpx.AsyncClient, key: str, body: Mapping[str, Any], **headers: str
) -> httpx.Response:
    return await client.post(CALLS, json=dict(body), headers={**bearer(key), **headers})
