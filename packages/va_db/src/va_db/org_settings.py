"""Cached reads of org settings (specs/04-auth-tenancy-rbac.md §7; specs/03-data-model.md §11).

Services read an org's settings through :class:`OrgSettingsReader`: Redis ``orgsettings:{org_id}`` holds the
stored sparse ``orgs.settings`` JSON for 60 s, and a miss reads the ``orgs`` row on the global session and
caches it. The cache is an optimization only: Redis errors and unreadable cached values are logged and the
database answers instead. Every settings write calls :meth:`OrgSettingsReader.invalidate` after its
transaction commits, so readers see the new values on their next read.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from redis.asyncio import Redis
from redis.exceptions import RedisError

from va_common import json
from va_common.logging import get_logger
from va_db.repositories.orgs import OrgRepo
from va_db.session import Database
from va_domain.org import OrgSettings, load_settings

__all__ = ["CACHE_TTL_S", "OrgSettingsReader", "settings_cache_key"]

CACHE_TTL_S: Final = 60

_log = get_logger(__name__)


def settings_cache_key(org_id: uuid.UUID) -> str:
    """The Redis key caching the stored settings of ``org_id``."""
    return f"orgsettings:{org_id}"


class OrgSettingsReader:
    """Org settings with defaults filled, cached in Redis for :data:`CACHE_TTL_S` seconds.

    :meth:`get` opens its own global session, so never call it while another session of the same pool is
    open in the same task.
    """

    def __init__(self, database: Database, redis: Redis) -> None:
        self._database = database
        self._redis = redis

    async def get(self, org_id: uuid.UUID) -> OrgSettings | None:
        """The settings of ``org_id``; ``None`` when the org does not exist (that is never cached)."""
        cached = await self._cached(org_id)
        if cached is not None:
            return cached
        async with self._database.global_session() as session:
            org = await OrgRepo(session).get(org_id)
            stored: dict[str, Any] | None = None if org is None else dict(org.settings)
        if stored is None:
            return None
        settings = load_settings(stored)
        await self._store(org_id, stored)
        return settings

    async def invalidate(self, org_id: uuid.UUID) -> None:
        """Drop the cached settings of ``org_id``; Redis errors are logged, never raised."""
        try:
            await self._redis.delete(settings_cache_key(org_id))
        except (RedisError, OSError) as exc:
            _log.warning("org_settings_cache_unavailable", org_id=str(org_id), error=type(exc).__name__)

    async def _cached(self, org_id: uuid.UUID) -> OrgSettings | None:
        try:
            raw = await self._redis.get(settings_cache_key(org_id))
        except (RedisError, OSError) as exc:
            _log.warning("org_settings_cache_unavailable", org_id=str(org_id), error=type(exc).__name__)
            return None
        if raw is None:
            return None
        try:
            # model_validate rejects anything but a JSON object, so a cached list or string is invalid too.
            return OrgSettings.model_validate(json.loads(raw))
        except ValueError:  # malformed JSON or a ValidationError (a ValueError subclass)
            _log.warning("org_settings_cache_invalid", org_id=str(org_id))
            return None

    async def _store(self, org_id: uuid.UUID, stored: dict[str, Any]) -> None:
        try:
            await self._redis.set(settings_cache_key(org_id), json.dumps(stored), ex=CACHE_TTL_S)
        except (RedisError, OSError) as exc:
            _log.warning("org_settings_cache_unavailable", org_id=str(org_id), error=type(exc).__name__)
