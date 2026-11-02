"""``DbCallContextLoader`` against a migrated Postgres: Redis context, rebuild from ``calls.config``, org from
the primary leg, status and identity refusals, infrastructure failures (specs/07 §3 step 3; specs/28 §6.2;
specs/07 §3 Decision P05-T07b)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import fakeredis
import pytest
from call_rows import SeededCall, seed_call, seed_leg
from media_helpers import call_context
from migration_helpers import migrated_postgres
from resolved_configs import resolved_config
from service_helpers import json_log_stream, log_events
from tenant_rows import open_database

from va_common.time import SystemClock
from va_db.call_context import CallContextStore
from va_db.session import Database
from va_domain.call_context import CallContext, call_context_key
from va_media.context import ContextRefusedError, DbCallContextLoader

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def media_db_url() -> Iterator[str]:
    with migrated_postgres() as url:
        yield url


@pytest.fixture
async def database(media_db_url: str) -> AsyncIterator[Database]:
    async with open_database(media_db_url) as db:
        yield db


def loader_for(database: Database, redis: fakeredis.FakeAsyncRedis) -> DbCallContextLoader:
    return DbCallContextLoader(database, CallContextStore(redis), SystemClock())


async def seeded(
    database: Database, *, status: Any = "dialing", leg_role: str = "primary"
) -> tuple[SeededCall, str]:
    call = await seed_call(database, status=status, config=resolved_config())
    control_id = f"cc-{uuid.uuid4().hex}"
    await seed_leg(database, call, control_id, role=leg_role)
    return call, control_id


async def refusal(loader: DbCallContextLoader, call_id: uuid.UUID, control_id: str) -> ContextRefusedError:
    with pytest.raises(ContextRefusedError) as caught:
        await loader.load(call_id, control_id)
    return caught.value


async def test_stored_context_is_used_after_the_status_check(database: Database) -> None:
    redis = fakeredis.FakeAsyncRedis()
    call, control_id = await seeded(database, status="in_progress")
    stored = call_context(call.call_id, org_id=call.org_id)
    await CallContextStore(redis).save(stored)
    loaded = await loader_for(database, redis).load(call.call_id, control_id)
    assert loaded == stored


async def test_missing_context_is_rebuilt_from_the_row_and_saved(database: Database) -> None:
    stream = json_log_stream()
    redis = fakeredis.FakeAsyncRedis()
    call, control_id = await seeded(database, status="ringing")
    loaded = await loader_for(database, redis).load(call.call_id, control_id)
    assert (loaded.call_id, loaded.org_id, loaded.to_number) == (call.call_id, call.org_id, "+14155550100")
    raw = await redis.get(call_context_key(call.call_id))
    assert raw is not None
    assert CallContext.model_validate_json(raw) == loaded
    assert 0 < await redis.ttl(call_context_key(call.call_id)) <= loaded.ttl_s()
    event = next(e for e in log_events(stream) if e["event"] == "media.context_rebuilt")
    assert event["org_id"] == str(call.org_id)


async def test_save_failure_after_a_rebuild_is_logged(database: Database) -> None:
    stream = json_log_stream()
    redis = fakeredis.FakeAsyncRedis()
    call, control_id = await seeded(database)

    class ReadOnlyStore(CallContextStore):
        async def save(self, context: CallContext) -> None:  # noqa: ARG002 - every save fails
            msg = "redis down"
            raise ConnectionError(msg)

    loader = DbCallContextLoader(database, ReadOnlyStore(redis), SystemClock())
    loaded = await loader.load(call.call_id, control_id)
    assert loaded.call_id == call.call_id
    assert any(e["event"] == "media.context_save_failed" for e in log_events(stream))


@pytest.mark.parametrize("status", ["queued", "completed", "failed", "transferred"])
async def test_calls_that_are_not_live_are_refused(database: Database, status: str) -> None:
    call, control_id = await seeded(database, status=status)
    caught = await refusal(loader_for(database, fakeredis.FakeAsyncRedis()), call.call_id, control_id)
    assert (caught.reason, caught.detail) == ("call_status", status)


async def test_status_is_checked_even_with_a_stored_context(database: Database) -> None:
    redis = fakeredis.FakeAsyncRedis()
    call, control_id = await seeded(database, status="completed")
    await CallContextStore(redis).save(call_context(call.call_id, org_id=call.org_id))
    caught = await refusal(loader_for(database, redis), call.call_id, control_id)
    assert caught.reason == "call_status"


async def test_unknown_or_mismatched_legs_are_refused(database: Database) -> None:
    loader = loader_for(database, fakeredis.FakeAsyncRedis())
    call, _ = await seeded(database)
    _, other_control_id = await seeded(database)
    transfer, transfer_control_id = await seeded(database, leg_role="transfer_target")
    assert (await refusal(loader, call.call_id, "cc-unknown")).reason == "call_unknown"
    assert (await refusal(loader, call.call_id, other_control_id)).detail == "leg_mismatch"
    assert (await refusal(loader, transfer.call_id, transfer_control_id)).reason == "call_unknown"


async def test_context_of_another_org_is_refused(database: Database) -> None:
    redis = fakeredis.FakeAsyncRedis()
    call, control_id = await seeded(database)
    await CallContextStore(redis).save(call_context(call.call_id, org_id=uuid.uuid4()))
    caught = await refusal(loader_for(database, redis), call.call_id, control_id)
    assert (caught.reason, caught.detail) == ("call_unknown", "call_missing")


async def test_redis_failure_refuses_with_redis(database: Database) -> None:
    server = fakeredis.FakeServer()
    server.connected = False
    call, control_id = await seeded(database)
    caught = await refusal(
        loader_for(database, fakeredis.FakeAsyncRedis(server=server)), call.call_id, control_id
    )
    assert caught.reason == "redis"


async def test_database_failure_refuses_with_database(media_db_url: str) -> None:
    missing = media_db_url.rsplit("/", 1)[0] + "/no_such_database"
    async with open_database(missing) as broken:
        caught = await refusal(loader_for(broken, fakeredis.FakeAsyncRedis()), uuid.uuid4(), "cc-x")
    assert caught.reason == "database"
