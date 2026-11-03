"""Retry schedule and attempt timeouts (specs/19-events-and-webhooks.md §8, §6)."""

from __future__ import annotations

import random

import pytest

from va_webhooks.schedule import (
    MAX_ATTEMPTS,
    RETRY_DELAYS_S,
    Timeouts,
    retry_delay_s,
    timeout_for,
)


class FixedRandom(random.Random):
    def __init__(self, value: float) -> None:
        super().__init__(0)
        self.value = value

    def random(self) -> float:
        return self.value


def test_schedule_matches_the_spec_table() -> None:
    assert RETRY_DELAYS_S == (10, 30, 120, 600, 1800, 7200, 21600, 54000)
    assert MAX_ATTEMPTS == 9
    assert sum(RETRY_DELAYS_S) == 85_360  # ≈ 23.7 h


@pytest.mark.parametrize("attempt", range(1, 9))
def test_delay_is_the_table_value_within_ten_percent(attempt: int) -> None:
    base = RETRY_DELAYS_S[attempt - 1]
    assert retry_delay_s(attempt, FixedRandom(0.0)) == pytest.approx(base * 0.9)
    assert retry_delay_s(attempt, FixedRandom(0.5)) == pytest.approx(base)
    assert retry_delay_s(attempt, FixedRandom(1.0)) == pytest.approx(base * 1.1)
    drawn = retry_delay_s(attempt, random.Random(attempt))  # noqa: S311 - jitter only
    assert drawn is not None
    assert base * 0.9 <= drawn <= base * 1.1


def test_after_the_ninth_attempt_the_delivery_is_dead() -> None:
    assert retry_delay_s(9, FixedRandom(0.5)) is None
    assert retry_delay_s(12, FixedRandom(0.5)) is None
    with pytest.raises(ValueError, match="attempt must be at least 1"):
        retry_delay_s(0, FixedRandom(0.5))


def test_batch_webhooks_time_out_after_ten_seconds_others_fifteen() -> None:
    assert timeout_for("batch.completed") == 10.0
    assert timeout_for("call.completed") == 15.0
    assert timeout_for("sms.received") == 15.0
    short = Timeouts(batch_s=0.1, default_s=0.2)
    assert (timeout_for("batch.failed", short), timeout_for("call.ended", short)) == (0.1, 0.2)
