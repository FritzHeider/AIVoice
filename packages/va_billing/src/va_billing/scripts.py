"""The limit Lua scripts (``redis_scripts/*.lua``, specs/24 §6) behind typed async wrappers.

Each script is registered once per client (``EVALSHA``, re-sent automatically on ``NOSCRIPT``). Lua returns
integers only; a cap of ``None`` (unlimited) travels as ``-1`` so unlimited counters still count usage.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from typing import Final

from redis.asyncio import Redis

__all__ = ["CappedResult", "LimitScripts", "load_script"]

_UNLIMITED: Final = -1


def load_script(name: str) -> str:
    """The source of ``redis_scripts/<name>.lua``."""
    return resources.files("va_billing").joinpath("redis_scripts", f"{name}.lua").read_text(encoding="utf-8")


def _cap(cap: int | None) -> int:
    return _UNLIMITED if cap is None else max(cap, 0)


@dataclass(frozen=True, slots=True)
class CappedResult:
    """Outcome of one capped increment: ``value`` is the counter after the call."""

    ok: bool
    value: int


class LimitScripts:
    """``capped_incr``, ``counter_release`` and ``concurrency_acquire`` on one Redis client."""

    def __init__(self, redis: Redis) -> None:
        self._capped_incr = redis.register_script(load_script("capped_incr"))
        self._counter_release = redis.register_script(load_script("counter_release"))
        self._concurrency_acquire = redis.register_script(load_script("concurrency_acquire"))

    async def capped_incr(self, key: str, cap: int | None, ttl_s: int) -> CappedResult:
        """Increment ``key`` unless that would exceed ``cap``; the first increment sets the TTL."""
        ok, value = await self._capped_incr(keys=[key], args=[_cap(cap), ttl_s])
        return CappedResult(ok=bool(ok), value=int(value))

    async def counter_release(self, key: str) -> int:
        """Undo one increment of ``key`` (never below 0, never creates the key); the value after."""
        return int(await self._counter_release(keys=[key], args=[]))

    async def concurrency_acquire(
        self, key: str, member: str, cap: int | None, *, now_ms: int, expiry_ms: int
    ) -> bool:
        """Drop expired members, then hold a slot for ``member`` until ``expiry_ms`` unless the set is full.

        Re-acquiring a member that already holds a slot only refreshes its expiry (idempotent).
        """
        held = await self._concurrency_acquire(keys=[key], args=[member, _cap(cap), now_ms, expiry_ms])
        return bool(held)
