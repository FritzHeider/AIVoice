"""``amd_ivr_mode`` (specs/30 §5): an ``ivr_mode`` call to a phone menu sends the precall DTMF ``1w2``
with the pause and takes no voicemail action; ``ivr_mode`` disables AMD, so the machine-like answer yields no
verdict at all (Decision P05-T17c under specs/30 §5)."""

from __future__ import annotations

from typing import Final

import pytest
from e2e_harness import Harness, iso_ms

from va_media.voicemail import VOICEMAIL_MESSAGE

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(600)]

TASK: Final = "You are calling Bella Pasta's phone menu to reach the sales team."
DTMF_GAP_MS: Final = 500


async def test_amd_ivr_mode(harness: Harness) -> None:
    org = await harness.create_org()
    scenario = await harness.load_scenario("amd_ivr_mode")
    call_id = await harness.send_task_call(
        org, scenario, TASK, ivr_mode=True, precall_dtmf_sequence="1w2", voicemail={"action": "hangup"}
    )
    finished, summary = await harness.finished_leg(org, call_id, scenario.number)
    assert not finished.failures(scenario), (finished.call, finished.events, finished.timeline)
    call = finished.call
    dtmf = summary["dtmf_sent"]
    assert [entry["digits"] for entry in dtmf] == ["1", "2"], dtmf
    assert iso_ms(dtmf[1]["at"]) - iso_ms(dtmf[0]["at"]) >= DTMF_GAP_MS, dtmf
    assert iso_ms(dtmf[0]["at"]) >= iso_ms(summary["answered_at"]), summary
    assert not [e for e in summary["events"] if e["event_type"].startswith("call.machine.")], summary
    assert not [event for event in finished.events if event["message"] == VOICEMAIL_MESSAGE], finished.events
    assert call["answered_by"] != "voicemail", call
    assert not str(call["end_reason"]).startswith("voicemail"), call
    assert call["voicemail"] is None, call
