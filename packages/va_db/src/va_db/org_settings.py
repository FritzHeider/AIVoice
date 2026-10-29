"""Cached reads of org settings (specs/04-auth-tenancy-rbac.md §7; specs/03-data-model.md §11).

Services read an org's settings through :class:`OrgSettingsReader`: Redis ``orgsettings:{org_id}`` holds the
stored sparse ``orgs.settings`` JSON for 60 s, and a miss reads the ``orgs`` row on the global session and
caches it. The cache is an optimization only: Redis errors and unreadable cached values are logged and the
database answers instead. Every settings write calls :meth:`OrgSettingsReader.invalidate` after its
transaction commits, so readers see the new values on their next read. Request gates (IP allowlist, session
window) read through :meth:`OrgSettingsReader.get_lenient`, which ignores stored keys that no longer validate
(an out-of-band edit) instead of failing every request of the org, so a settings PATCH can still repair them.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from va_common import json
from va_common.logging import get_logger
from va_db.repositories.orgs import OrgRepo
from va_db.session import Database
from va_domain.org import OrgSettings, load_settings

__all__ = ["CACHE_TTL_S", "OrgSettingsReader", "settings_cache_key", "valid_part"]

CACHE_TTL_S: Final = 60

_log = get_logger(__name__)


def settings_cache_key(org_id: uuid.UUID) -> str:
    """The Redis key caching the stored settings of ``org_id``."""
    return f"orgsettings:{org_id}"


def valid_part(stored: dict[str, Any]) -> tuple[OrgSettings, list[str]]:
    """``stored`` validated without the top-level keys that fail validation, and those keys (sorted)."""
    remaining = dict(stored)
    dropped: set[str] = set()
    while True:
        try:
            return OrgSettings.model_validate(remaining), sorted(dropped)
        except ValidationError as exc:
            bad = {str(error["loc"][0]) for error in exc.errors() if error["loc"]} & remaining.keys()
            if not bad:  # an object-level error: nothing stored can be trusted
                return OrgSettings(), sorted(stored)
            dropped |= bad
            remaining = {key: value for key, value in remaining.items() if key not in bad}


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
        stored = await self._load(org_id)
        if stored is None:
            return None
        settings = load_settings(stored)
        await self._store(org_id, stored)
        return settings

    async def get_lenient(self, org_id: uuid.UUID) -> OrgSettings | None:
        """Like :meth:`get`, but stored keys that fail validation read as their defaults (warning
        ``org_settings_invalid`` with the key names) instead of raising ``ValidationError``."""
        try:
            return await self.get(org_id)
        except ValidationError:
            stored = await self._load(org_id)
        if stored is None:
            return None
        settings, dropped = valid_part(stored)
        _log.warning("org_settings_invalid", org_id=str(org_id), keys=dropped)
        return settings

    async def invalidate(self, org_id: uuid.UUID) -> None:
        """Drop the cached settings of ``org_id``; Redis errors are logged, never raised."""
        try:
            await self._redis.delete(settings_cache_key(org_id))
        except (RedisError, OSError) as exc:
            _log.warning("org_settings_cache_unavailable", org_id=str(org_id), error=type(exc).__name__)

    async def _load(self, org_id: uuid.UUID) -> dict[str, Any] | None:
        """The stored sparse settings of ``org_id`` from the database; ``None`` when the org is gone."""
        async with self._database.global_session() as session:
            org = await OrgRepo(session).get(org_id)
            return None if org is None else dict(org.settings)

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
