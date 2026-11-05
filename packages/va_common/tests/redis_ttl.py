"""Redis TTL assertions that do not race the wall clock.

fakeredis (and real Redis) store an expiry as a wall-clock deadline and ``TTL`` floors the remaining
seconds, so a test that asserts ``ttl == N`` fails whenever any time passes between the write and the
read (a loaded xdist worker).
Assert the remaining milliseconds against a window instead.
"""

from __future__ import annotations

from typing import Final

from redis.asyncio import Redis

__all__ = ["TTL_SLACK_MS", "assert_expires_in"]

TTL_SLACK_MS: Final = 30_000
"""How long a test may stall between the write and the read; far longer than any real stall."""


async def assert_expires_in(redis: Redis, key: str, ttl_s: int, *, slack_ms: int = TTL_SLACK_MS) -> None:
    """Assert that ``key`` was set to expire in ``ttl_s`` seconds.

    The upper bound proves the expiry is not longer than ``ttl_s``; the lower bound proves it is longer than
    ``ttl_s`` minus ``slack_ms`` (a missing key, a key without expiry, or a shorter TTL fail).
    For a TTL at or below the slack the lower bound is 0: the key must exist and expire, but its length is
    only bounded above (``PTTL`` answers -2 for a missing key and -1 for one without expiry).
    """
    upper = ttl_s * 1000
    lower = max(0, upper - slack_ms)
    remaining = await redis.pttl(key)
    assert lower < remaining <= upper, f"{key}: pttl {remaining} ms, expected within ({lower}, {upper}] ms"
