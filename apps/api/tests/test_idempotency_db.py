"""``DbIdempotencyStore`` over a migrated Postgres (specs/04 §12): a tenant session per call, RLS applied."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from docker_probe import docker_available
from migration_helpers import fresh_database, upgrade
from pg_container import running_postgres
from tenant_rows import OrgFactory, open_database

from va_api.idempotency import DbIdempotencyStore
from va_common.time import FrozenClock
from va_db.repositories import ClaimOutcome, IdempotencyClaim, IdempotencyConflict, IdempotencyReplay
from va_db.session import Database

pytestmark = [pytest.mark.integration, pytest.mark.timeout(180)]

_PATH = "/v1/calls"


@pytest.fixture(scope="module")
def migrated_url() -> Iterator[str]:
    if not docker_available():
        pytest.skip("docker unavailable")
    with running_postgres() as server_url, fresh_database(server_url) as url:
        upgrade(url)
        yield url


@pytest.fixture
async def database(migrated_url: str) -> AsyncIterator[Database]:
    async with open_database(migrated_url) as db:
        yield db


async def test_claim_complete_replay_release(database: Database) -> None:
    org_a, org_b = await OrgFactory(database).create(), await OrgFactory(database).create()
    clock = FrozenClock()
    store = DbIdempotencyStore(database, clock)

    async def claim(org: uuid.UUID, key: str, digest: str = "a" * 64) -> ClaimOutcome:
        return await store.claim(org, key=key, method="POST", path=_PATH, request_hash=digest)

    assert await claim(org_a, "k1") == IdempotencyClaim("k1")
    assert await claim(org_a, "k1") is IdempotencyConflict.IN_PROGRESS
    assert await claim(org_a, "k1", "b" * 64) is IdempotencyConflict.KEY_REUSED
    assert await claim(org_b, "k1") == IdempotencyClaim("k1")

    await store.complete(org_a, "k1", status=201, body={"call_id": "c1", "status": "success"})
    clock.advance(30)
    assert await claim(org_a, "k1") == IdempotencyReplay(201, {"call_id": "c1", "status": "success"})

    await store.release(org_a, "k1")  # a stored response is never released
    await store.release(org_b, "k1")
    assert await claim(org_a, "k1") == IdempotencyReplay(201, {"call_id": "c1", "status": "success"})
    assert await claim(org_b, "k1") == IdempotencyClaim("k1")
