"""The resolved pathway graph cache keys (specs/03-data-model.md §11 ``pathway:{id}:v:{env}``).

Rules: specs/12 §4 Decision P07-T09b. Only pinned versions are cached; publishing to an environment and
promoting to production delete the environment's key after the transaction commits. The cache is an
optimization: a Redis failure is logged and the write still succeeds (the 10 min TTL bounds staleness).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Final

from redis.asyncio import Redis
from redis.exceptions import RedisError

from va_common.logging import get_logger
from va_db.repositories import PinEnvironment

__all__ = ["PATHWAY_CACHE_ENVIRONMENTS", "invalidate_pathway_cache", "pathway_cache_key"]

PATHWAY_CACHE_ENVIRONMENTS: Final[tuple[PinEnvironment, ...]] = ("production", "staging")

_log = get_logger(__name__)


def pathway_cache_key(pathway_id: uuid.UUID, environment: PinEnvironment) -> str:
    """The Redis key caching the graph pinned to ``environment``."""
    return f"pathway:{pathway_id}:v:{environment}"


async def invalidate_pathway_cache(
    redis: Redis, pathway_id: uuid.UUID, environments: Iterable[PinEnvironment]
) -> list[PinEnvironment]:
    """Delete the cached graphs of ``environments``; returns the environments whose delete failed."""
    failed: list[PinEnvironment] = []
    for environment in environments:
        try:
            await redis.delete(pathway_cache_key(pathway_id, environment))
        except (RedisError, OSError) as exc:
            failed.append(environment)
            _log.warning(
                "pathway_cache_unavailable",
                pathway_id=str(pathway_id),
                environment=environment,
                error=type(exc).__name__,
            )
    return failed
