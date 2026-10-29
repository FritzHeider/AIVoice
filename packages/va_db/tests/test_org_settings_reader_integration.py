"""``OrgSettingsReader``: Redis-cached org settings with a database fallback (specs/04 §7; specs/03 §11)."""

from __future__ import annotations

import uuid
from typing import Any

import fakeredis
import pytest
from identity_db import IdentityDb
from model_factories import make_org
from sqlalchemy import update

from va_common import json
from va_common.ids import new_id
from va_db import org_settings as org_settings_module
from va_db.models import Org
from va_db.org_settings import CACHE_TTL_S, OrgSettingsReader, settings_cache_key
from va_domain.org import OrgSettings, load_settings

pytestmark = pytest.mark.integration


class RecordingLog:
    """Stands in for the module logger; keeps the warning events."""

    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **fields: Any) -> None:
        self.warnings.append((event, fields))


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> RecordingLog:
    recording = RecordingLog()
    monkeypatch.setattr(org_settings_module, "_log", recording)
    return recording


async def new_org(identity_db: IdentityDb, settings: dict[str, Any]) -> uuid.UUID:
    async with identity_db.database.system_session() as session:
        return (await make_org(session, settings=settings)).id


async def set_settings(identity_db: IdentityDb, org_id: uuid.UUID, settings: dict[str, Any]) -> None:
    async with identity_db.database.system_session() as session:
        await session.execute(update(Org).where(Org.id == org_id).values(settings=settings))


async def cached_json(redis: fakeredis.FakeAsyncRedis, org_id: uuid.UUID) -> Any:
    raw = await redis.get(settings_cache_key(org_id))
    assert raw is not None
    return json.loads(raw)


def disconnected_redis() -> fakeredis.FakeAsyncRedis:
    server = fakeredis.FakeServer()
    server.connected = False
    return fakeredis.FakeAsyncRedis(server=server)


async def test_a_miss_reads_the_database_and_caches_the_sparse_object(identity_db: IdentityDb) -> None:
    redis = fakeredis.FakeAsyncRedis()
    org_id = await new_org(identity_db, {"timezone": "America/Chicago", "llm": {"temperature_default": 0.2}})
    reader = OrgSettingsReader(identity_db.database, redis)
    settings = await reader.get(org_id)
    assert settings == load_settings({"timezone": "America/Chicago", "llm": {"temperature_default": 0.2}})
    key = settings_cache_key(org_id)
    assert key == f"orgsettings:{org_id}"
    assert await cached_json(redis, org_id) == {
        "timezone": "America/Chicago",
        "llm": {"temperature_default": 0.2},
    }
    assert 0 < await redis.ttl(key) <= CACHE_TTL_S


async def test_empty_settings_read_as_the_defaults(identity_db: IdentityDb) -> None:
    redis = fakeredis.FakeAsyncRedis()
    org_id = await new_org(identity_db, {})
    assert await OrgSettingsReader(identity_db.database, redis).get(org_id) == OrgSettings()
    assert await cached_json(redis, org_id) == {}


async def test_a_hit_is_served_from_the_cache_until_invalidated(identity_db: IdentityDb) -> None:
    redis = fakeredis.FakeAsyncRedis()
    org_id = await new_org(identity_db, {"timezone": "America/Chicago"})
    reader = OrgSettingsReader(identity_db.database, redis)
    await reader.get(org_id)
    await set_settings(identity_db, org_id, {"timezone": "America/Denver"})
    cached = await reader.get(org_id)
    assert cached is not None
    assert cached.timezone == "America/Chicago"
    await reader.invalidate(org_id)
    assert await redis.get(settings_cache_key(org_id)) is None
    fresh = await reader.get(org_id)
    assert fresh is not None
    assert fresh.timezone == "America/Denver"


async def test_an_unknown_org_is_none_and_not_cached(identity_db: IdentityDb) -> None:
    redis = fakeredis.FakeAsyncRedis()
    missing = new_id()
    assert await OrgSettingsReader(identity_db.database, redis).get(missing) is None
    assert await redis.get(settings_cache_key(missing)) is None


async def test_redis_down_falls_back_to_the_database(identity_db: IdentityDb, log: RecordingLog) -> None:
    org_id = await new_org(identity_db, {"default_record": True})
    reader = OrgSettingsReader(identity_db.database, disconnected_redis())
    settings = await reader.get(org_id)
    assert settings is not None
    assert settings.default_record is True
    await reader.invalidate(org_id)
    events = [event for event, _ in log.warnings]
    assert events == ["org_settings_cache_unavailable"] * 3  # read, store, invalidate
    assert all(fields == {"org_id": str(org_id), "error": "ConnectionError"} for _, fields in log.warnings)


@pytest.mark.parametrize(
    "cached",
    [
        b"{not json",
        b"[1, 2]",
        b'"text"',
        json.dumps({"timezone": "Mars/Olympus_Mons"}),
        json.dumps({"bogus": 1}),
    ],
)
async def test_an_unreadable_cached_value_falls_back_to_the_database(
    identity_db: IdentityDb, log: RecordingLog, cached: bytes
) -> None:
    redis = fakeredis.FakeAsyncRedis()
    org_id = await new_org(identity_db, {"timezone": "America/Chicago"})
    await redis.set(settings_cache_key(org_id), cached)
    settings = await OrgSettingsReader(identity_db.database, redis).get(org_id)
    assert settings is not None
    assert settings.timezone == "America/Chicago"
    assert log.warnings == [("org_settings_cache_invalid", {"org_id": str(org_id)})]
    assert await cached_json(redis, org_id) == {"timezone": "America/Chicago"}
