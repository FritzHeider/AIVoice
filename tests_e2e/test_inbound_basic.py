"""``inbound_basic`` (specs/30 §5): a caller rings an org number, the task runner greets with
``first_sentence``, answers once, and the caller hangs up (pathway runners arrive in phase 07; specs/30 §5
Decision P05-T17b)."""

from __future__ import annotations

from typing import Final

import pytest
from e2e_harness import Harness, random_e164

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(600)]

FIRST_SENTENCE: Final = "Thanks for calling Bella Pasta, how can I help?"
PROMPT: Final = "You are the host at Bella Pasta. Help callers book a table."
ANSWER_WITHIN_MS: Final = 1000


async def test_inbound_basic(harness: Harness) -> None:
    caller = random_e164()
    scenario = await harness.load_scenario("inbound_basic", number=caller)
    e164, number_id = await harness.order_inbound_number()
    inbound_config = {"prompt": f"{PROMPT} {scenario.marker}", "first_sentence": FIRST_SENTENCE}
    org = await harness.create_org(
        numbers=[{"e164": e164, "telnyx_phone_number_id": number_id, "inbound_config": inbound_config}]
    )
    ccid = await harness.inbound(e164, caller, scenario.id)
    call_id = await harness.only_call(org)
    finished = await harness.finished(org, call_id, ccid=ccid)
    call, timeline = finished.call, finished.timeline

    assert not finished.failures(scenario), (call, timeline)
    assert timeline["answered_ms"] is not None
    assert timeline["answered_ms"] - timeline["initiated_ms"] <= ANSWER_WITHIN_MS, timeline
    assert (call["direction"], call["inbound"], call["to"], call["from"]) == ("inbound", True, e164, caller)
    # The task runner (no pathway, persona, or agent).
    assert (call["pathway_id"], call["persona_id"], call["agent_id"]) == (None, None, None)
    spoken = await harness.tts_texts()
    assert spoken, "no agent speech was synthesized"
    assert spoken[0] == FIRST_SENTENCE
    assert call["transcripts"][0]["user"] == "assistant"
    assert call["transcripts"][0]["text"] == FIRST_SENTENCE
