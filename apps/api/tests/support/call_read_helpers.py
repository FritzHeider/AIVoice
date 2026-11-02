"""Seeding helpers of the call read tests: an org with a ``*`` key and calls created at fixed times."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from call_rows import seed_call
from session_helpers import SessionContainer, store_api_key

__all__ = ["BASE", "SeededOrg", "an_org"]

BASE: Final = datetime(2026, 10, 20, 12, tzinfo=UTC)


class SeededOrg:
    """An org with a ``*`` key and the calls seeded into it (in seeding order)."""

    def __init__(self, org_id: uuid.UUID, key: str) -> None:
        self.org_id = org_id
        self.key = key
        self.calls: list[uuid.UUID] = []


async def an_org(container: SessionContainer, *calls: dict[str, Any]) -> SeededOrg:
    """A fresh org holding one call per ``calls`` entry; call ``i`` is created at ``BASE + i minutes`` unless
    the entry sets ``created_at`` (and is ``completed`` unless it sets ``status``)."""
    first = await seed_call(container.database, status="completed", created_at=BASE - timedelta(days=30))
    key, _ = await store_api_key(container, first.org_id)
    org = SeededOrg(first.org_id, key)
    for index, values in enumerate(calls):
        merged = {"status": "completed", "created_at": BASE + timedelta(minutes=index), **values}
        seeded = await seed_call(container.database, org_id=org.org_id, **merged)
        org.calls.append(seeded.call_id)
    return org
