from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from va_common.time import Clock, FrozenClock, SystemClock, ensure_utc, utcnow

START = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def test_utcnow_is_aware_utc() -> None:
    now = utcnow()
    assert now.tzinfo is UTC
    assert abs((datetime.now(UTC) - now).total_seconds()) < 5


def test_ensure_utc_converts_offset_datetimes() -> None:
    local = datetime(2026, 3, 1, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    converted = ensure_utc(local)
    assert converted == START
    assert converted.utcoffset() == timedelta(0)


def test_ensure_utc_rejects_naive() -> None:
    with pytest.raises(ValueError, match="naive"):
        ensure_utc(datetime(2026, 3, 1, 12, 0))  # noqa: DTZ001


async def test_system_clock_reads_real_time() -> None:
    clock: Clock = SystemClock()
    first = clock.monotonic()
    await clock.sleep(0)
    assert clock.monotonic() >= first
    assert clock.now().tzinfo is UTC


def test_frozen_clock_default_start() -> None:
    clock = FrozenClock()
    assert clock.now() == datetime(2026, 1, 1, tzinfo=UTC)
    assert clock.monotonic() == 0.0


def test_frozen_clock_normalises_start_to_utc() -> None:
    clock = FrozenClock(datetime(2026, 3, 1, 14, 0, tzinfo=timezone(timedelta(hours=2))))
    assert clock.now() == START
    assert clock.now().utcoffset() == timedelta(0)


def test_frozen_clock_advance_moves_wall_and_monotonic() -> None:
    clock = FrozenClock(START)
    clock.advance(1.5)
    clock.advance(timedelta(minutes=1))
    assert clock.now() == START + timedelta(seconds=61.5)
    assert clock.monotonic() == pytest.approx(61.5)


def test_frozen_clock_rejects_negative_advance() -> None:
    clock = FrozenClock(START)
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(-1)
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(timedelta(seconds=-1))
    assert clock.now() == START


def test_frozen_clock_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="naive"):
        FrozenClock(datetime(2026, 1, 1))  # noqa: DTZ001
    clock = FrozenClock(START)
    with pytest.raises(ValueError, match="naive"):
        clock.set(datetime(2026, 1, 1))  # noqa: DTZ001


def test_frozen_clock_set_jumps_wall_clock_only() -> None:
    clock = FrozenClock(START)
    clock.advance(3)
    target = datetime(2030, 1, 1, tzinfo=UTC)
    clock.set(target)
    assert clock.now() == target
    assert clock.monotonic() == 3.0


async def test_frozen_clock_sleep_advances_without_waiting() -> None:
    clock: Clock = FrozenClock(START)
    real_before = SystemClock().monotonic()
    await clock.sleep(5)
    assert clock.now() == START + timedelta(seconds=5)
    assert clock.monotonic() == 5.0
    assert SystemClock().monotonic() - real_before < 1.0
