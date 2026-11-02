"""``voicemail_leave_message`` (specs/30 §5): a machine answers and beeps 4 s later; the agent speaks the
voicemail message only after the beep, then hangs up (Decision P05-T17c under specs/30 §5)."""

from __future__ import annotations

from typing import Final

import pytest
from e2e_harness import Harness

from va_media.voicemail import VOICEMAIL_MESSAGE

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(600)]

TASK: Final = "You are Maya from Bella Pasta calling about a reservation."
MESSAGE: Final = "Hi, call us back"
BEEP_AT_MS: Final = 4000


async def test_voicemail_leave_message(harness: Harness) -> None:
    org = await harness.create_org()
    scenario = await harness.load_scenario("voicemail_leave_message")
    call_id = await harness.send_task_call(
        org,
        scenario,
        TASK,
        voicemail={"message": MESSAGE, "action": "leave_message"},
        background_track="none",
    )
    finished, summary = await harness.finished_leg(org, call_id, scenario.number)
    call, timeline = finished.call, finished.timeline

    assert not finished.failures(scenario), (call, finished.events, timeline)
    assert timeline["first_agent_speech_ms"] is not None, timeline
    assert timeline["first_agent_speech_ms"] - timeline["answered_ms"] >= BEEP_AT_MS, timeline
    assert await harness.tts_texts() == [MESSAGE]
    assert summary["hangup_source"] == "caller", summary
    [voicemail] = [event for event in finished.events if event["message"] == VOICEMAIL_MESSAGE]
    assert voicemail["data"]["action"] == "leave_message", voicemail
    assert call["call_ended_by"] == "ASSISTANT"
    assert call["voicemail"] == {"detected": True, "action_taken": "leave_message", "message_left": True}
    assert await harness.llm_requests(call_id) == []
