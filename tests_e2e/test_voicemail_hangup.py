"""``voicemail_hangup`` (specs/30 §5): a machine answers and the voicemail action ``hangup`` ends the call
within 2 s of the AMD result without any speech (Decision P05-T17c under specs/30 §5)."""

from __future__ import annotations

from typing import Final

import pytest
from e2e_harness import Harness, iso_ms

from va_media.voicemail import VOICEMAIL_MESSAGE

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(600)]

TASK: Final = "You are Maya from Bella Pasta calling about a reservation."
AMD_RESULT_EVENT: Final = "call.machine.premium.detection.ended"
HANGUP_WITHIN_MS: Final = 2000


async def test_voicemail_hangup(harness: Harness) -> None:
    org = await harness.create_org()
    scenario = await harness.load_scenario("voicemail_hangup")
    call_id = await harness.send_task_call(org, scenario, TASK, voicemail={"action": "hangup"})
    finished, summary = await harness.finished_leg(org, call_id, scenario.number)
    call = finished.call

    assert not finished.failures(scenario), (call, finished.events, finished.timeline)
    amd_at = next(iso_ms(e["at"]) for e in summary["events"] if e["event_type"] == AMD_RESULT_EVENT)
    hangup_at = next(iso_ms(c["at"]) for c in summary["commands"] if c["name"] == "hangup")
    assert 0 <= hangup_at - amd_at <= HANGUP_WITHIN_MS, summary
    assert await harness.tts_texts() == []
    assert await harness.llm_requests(call_id) == []
    [voicemail] = [event for event in finished.events if event["message"] == VOICEMAIL_MESSAGE]
    assert voicemail["data"]["action"] == "hangup", voicemail
    assert call["call_ended_by"] == "SYSTEM"
