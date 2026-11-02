"""``max_duration`` (specs/30 §5): with ``max_duration: 1`` an engaged call ends 60 s ± 1 s after the answer,
after the time-limit message (spoken 10 s before the limit, specs/07 §18)."""

from __future__ import annotations

from typing import Final

import pytest
from e2e_harness import Harness

from va_domain.org import OrgSettings

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(600)]

TASK: Final = "You are Maya from Bella Pasta. Keep the caller company."
LIMIT_MS: Final = 60_000
TOLERANCE_MS: Final = 1000


async def test_max_duration(harness: Harness) -> None:
    org = await harness.create_org()
    scenario = await harness.load_scenario("max_duration")
    call_id = await harness.send_task_call(org, scenario, TASK, max_duration=1)
    finished = await harness.finished(org, call_id, number=scenario.number)
    call, timeline = finished.call, finished.timeline

    assert not finished.failures(scenario), (call, timeline)
    connected_ms = timeline["ended_ms"] - timeline["answered_ms"]
    assert abs(connected_ms - LIMIT_MS) <= TOLERANCE_MS, timeline
    assert call["call_ended_by"] == "TIMEOUT"
    assert OrgSettings().max_duration_message in await harness.tts_texts()
    # The caller kept talking, so the no-input policy never ended the call first.
    assert sum(1 for turn in call["transcripts"] if turn["user"] == "user") >= 5
