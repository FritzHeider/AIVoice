"""``stop_via_api`` (specs/30 §5): ``POST /v1/calls/{id}/stop`` 10 s into a long call hangs up within 1 s."""

from __future__ import annotations

from typing import Final

import pytest
from e2e_harness import Harness

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(600)]

TASK: Final = "You are Maya from Bella Pasta. Chat with the caller about tonight's specials."
STOP_AFTER_MS: Final = 10_000
HANGUP_WITHIN_MS: Final = 1000


async def test_stop_via_api(harness: Harness) -> None:
    org = await harness.create_org()
    scenario = await harness.load_scenario("stop_via_api")
    call_id = await harness.send_task_call(org, scenario, TASK)
    sim = await harness.sim_call(scenario.number)
    ccid = sim["call_control_id"]
    await harness.wait_sim(ccid, lambda summary: summary["answered_at"] is not None, what="the answer")
    answered_ms = (await harness.timeline(ccid))["answered_ms"]
    await harness.wait_until_sim_ms(answered_ms + STOP_AFTER_MS)
    stop_ms = await harness.sim_clock_ms()
    await harness.stop_call(org, call_id)
    finished = await harness.finished(org, call_id, ccid=ccid)
    call, timeline = finished.call, finished.timeline

    assert not finished.failures(scenario), (call, timeline)
    assert timeline["ended_ms"] is not None
    assert timeline["ended_ms"] - stop_ms <= HANGUP_WITHIN_MS, (stop_ms, timeline)
    assert call["call_ended_by"] == "API"
