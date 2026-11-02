"""``worker_crash_recovery`` (specs/30 §5): the media worker container is killed mid-call; the call's
heartbeat expires, the ``CallWorkflow`` heartbeat watch ends it ``worker_crash`` and hangs up, and post-call
processing completes with the transcript written before the crash (Decision P05-T17d under specs/30 §5)."""

from __future__ import annotations

from typing import Any, Final

import pytest
from e2e_harness import Harness

from va_domain.call_object.mapping import CALL_ENDED_BY
from va_media.session import HEARTBEAT_TTL_S
from va_workflows.activities.liveness_inputs import HEARTBEAT_MISSING_AFTER_S, HEARTBEAT_POLL_INTERVAL_S

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(600)]

TASK: Final = "You are Maya from Bella Pasta. Help the caller book a table."
FIRST_SENTENCE: Final = "Hi, this is Maya from Bella Pasta."
REPLIED_STEP: Final = 2
TRANSCRIPTS_BEFORE_CRASH: Final = 2
ACTIVITIES_MS: Final = 2000
# Kill → hangup: the heartbeat key outlives the worker by its TTL, the watch needs it missing for the window
# (checked every poll interval), then end_call + hangup_call run.
HANGUP_WITHIN_MS: Final = round(
    (HEARTBEAT_TTL_S + HEARTBEAT_MISSING_AFTER_S + HEARTBEAT_POLL_INTERVAL_S) * 1000 + ACTIVITIES_MS
)
HANGUP_NOT_BEFORE_MS: Final = HEARTBEAT_MISSING_AFTER_S * 1000
LEG_END_WAIT_S: Final = 90.0


def replied(summary: dict[str, Any]) -> bool:
    script = summary["script"]
    steps = [] if script is None else script["steps"]
    return len(steps) > REPLIED_STEP and steps[REPLIED_STEP]["status"] == "ok"


async def test_worker_crash_recovery(harness: Harness) -> None:
    org = await harness.create_org()
    scenario = await harness.load_scenario("worker_crash_recovery")
    call_id = await harness.send_task_call(org, scenario, TASK, first_sentence=FIRST_SENTENCE)
    ccid = (await harness.sim_call(scenario.number))["call_control_id"]
    await harness.wait_sim(ccid, replied, what="the agent's reply to the caller", timeout_s=60)
    await harness.wait_call(
        org,
        call_id,
        lambda call: len(call["transcripts"]) >= TRANSCRIPTS_BEFORE_CRASH,
        what="transcripts written before the crash",
    )
    try:
        kill_ms = await harness.kill_media()
        await harness.wait_sim(
            ccid, lambda summary: summary["ended_at"] is not None, what="the hangup", timeout_s=LEG_END_WAIT_S
        )
    finally:
        await harness.restart_media()
    finished = await harness.finished(org, call_id, ccid=ccid)
    call, timeline = finished.call, finished.timeline
    summary = await harness.sim_summary(ccid)

    assert not finished.failures(scenario), (call, finished.events, timeline)
    hangup_after_ms = timeline["ended_ms"] - kill_ms
    print(f"worker_crash_recovery: kill -> hangup {hangup_after_ms} ms")  # noqa: T201
    assert HANGUP_NOT_BEFORE_MS <= hangup_after_ms <= HANGUP_WITHIN_MS, (kill_ms, timeline)
    hangups = [c for c in summary["commands"] if c["name"] == "hangup" and not c["duplicate"]]
    assert hangups, summary["commands"]
    assert summary["hangup_source"] == "caller", summary
    assert call["queue_status"] == "complete", call
    assert call["call_ended_by"] == CALL_ENDED_BY["error"], call
    assert len(call["transcripts"]) >= TRANSCRIPTS_BEFORE_CRASH, call["transcripts"]
    assert call["transcripts"][0]["text"] == FIRST_SENTENCE, call["transcripts"]
