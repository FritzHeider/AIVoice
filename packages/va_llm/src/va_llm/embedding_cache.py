"""The query embedding cache (specs/10-llm-gateway.md §10; specs/03 §11 ``emb:q:{sha256}``; Decision
2026-11-01 P03-T14b rule 6).

Values are the unit vector as an orjson list of floats, stored with ``SET ... EX 86400``. A cached value that
is not a vector of the configured length counts as a miss (the next embedding overwrites it). Redis failures
fail open: they are logged ``llm.embedding_cache_unavailable`` and the query is embedded without the cache.
"""

from __future__ import annotations

from typing import Final, Literal

import orjson
from redis.asyncio import Redis
from redis.exceptions import RedisError

from va_common.logging import get_logger
from va_llm.embedding_wire import is_vector, query_cache_key
from va_llm.metrics import LlmMetrics

__all__ = ["QUERY_CACHE_TTL_S", "QueryCache"]

QUERY_CACHE_TTL_S: Final = 86_400

_log = get_logger(__name__)


class QueryCache:
    """Reads and writes ``emb:q:{sha256(model|dims|text)}``; counts ``va_llm_embedding_cache_total``."""

    def __init__(self, redis: Redis, metrics: LlmMetrics, *, model: str, dimensions: int) -> None:
        self._redis = redis
        self._metrics = metrics
        self._model = model
        self._dimensions = dimensions

    def key(self, text: str) -> str:
        return query_cache_key(self._model, self._dimensions, text)

    def _unavailable(self, operation: Literal["get", "set"], exc: Exception) -> None:
        _log.warning("llm.embedding_cache_unavailable", operation=operation, error=type(exc).__name__)

    async def get(self, text: str) -> list[float] | None:
        """The cached vector of ``text`` (``None`` on a miss, a malformed value, or a Redis failure)."""
        try:
            raw = await self._redis.get(self.key(text))
        except (RedisError, OSError) as exc:
            self._unavailable("get", exc)
            self._metrics.embedding_cache("error")
            return None
        vector = self._decode(raw)
        self._metrics.embedding_cache("miss" if vector is None else "hit")
        return vector

    def _decode(self, raw: bytes | str | None) -> list[float] | None:
        if raw is None:
            return None
        try:
            value = orjson.loads(raw)
        except orjson.JSONDecodeError:
            return None
        if not is_vector(value, self._dimensions):
            return None
        return [float(item) for item in value]

    async def put(self, text: str, vector: list[float]) -> None:
        """Cache ``vector`` for 24 h; a Redis failure is logged, never raised."""
        try:
            await self._redis.set(self.key(text), orjson.dumps(vector), ex=QUERY_CACHE_TTL_S)
        except (RedisError, OSError) as exc:
            self._unavailable("set", exc)
