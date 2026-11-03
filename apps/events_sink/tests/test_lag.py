"""``va_events_sink_lag_seconds``: age of the oldest unacknowledged entry (specs/19 §1 Decision P06-T05)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fakeredis import FakeAsyncRedis

from va_events_sink.lag import entry_ms, lag_seconds, oldest_unacked_ids, stream_lag

AT = datetime(2026, 11, 3, 9, tzinfo=UTC)
AT_MS = int(AT.timestamp() * 1000)


def test_entry_ms_is_the_id_prefix() -> None:
    assert entry_ms("1793684470283-12") == 1_793_684_470_283


@pytest.mark.parametrize(
    ("ids", "lag"),
    [
        ([], 0.0),
        ([f"{AT_MS - 2500}-0"], 2.5),
        ([f"{AT_MS - 1000}-3", f"{AT_MS - 7000}-0"], 7.0),
        ([f"{AT_MS + 900}-0"], 0.0),
    ],
)
def test_lag_is_the_oldest_age_never_negative(ids: list[str], lag: float) -> None:
    assert lag_seconds(ids, AT) == pytest.approx(lag)


async def test_pending_and_undelivered_entries_both_count() -> None:
    redis = FakeAsyncRedis()
    key = "events:calls"
    assert await oldest_unacked_ids(redis, key, "events-sink") == []
    await redis.xgroup_create(key, "events-sink", id="0", mkstream=True)
    assert await stream_lag(redis, key, "events-sink", AT) == 0.0
    for offset in (9000, 6000, 3000):
        await redis.xadd(key, {"payload": b"x"}, id=f"{AT_MS - offset}-0")
    assert await oldest_unacked_ids(redis, key, "events-sink") == [f"{AT_MS - 9000}-0"]
    await redis.xreadgroup("events-sink", "pod-a", {key: ">"}, count=2)
    assert await oldest_unacked_ids(redis, key, "events-sink") == [f"{AT_MS - 9000}-0", f"{AT_MS - 3000}-0"]
    assert await stream_lag(redis, key, "events-sink", AT) == pytest.approx(9.0)
    await redis.xack(key, "events-sink", f"{AT_MS - 9000}-0", f"{AT_MS - 6000}-0")
    assert await stream_lag(redis, key, "events-sink", AT) == pytest.approx(3.0)
    await redis.xreadgroup("events-sink", "pod-a", {key: ">"})
    await redis.xack(key, "events-sink", f"{AT_MS - 3000}-0")
    assert await stream_lag(redis, key, "events-sink", AT) == 0.0


async def test_another_groups_progress_is_ignored() -> None:
    redis = FakeAsyncRedis()
    key = "events:sms"
    await redis.xgroup_create(key, "worker", id="0", mkstream=True)
    await redis.xadd(key, {"payload": b"x"}, id=f"{AT_MS - 4000}-0")
    assert await oldest_unacked_ids(redis, key, "events-sink") == []
