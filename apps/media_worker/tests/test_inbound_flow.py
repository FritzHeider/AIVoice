"""An inbound call end to end in one process (specs/06 §7 Decision P05-T12c, specs/16 §4 Decision P05-T12d):
FakeTelnyx rings a number, the edge routes and answers it, the media worker converses (or plays the
maintenance message) and ends the call; the docker e2e harness of P05-T17 runs the same flow across
containers (``inbound_basic``)."""

from __future__ import annotations

import asyncio
from typing import Final

import pytest
from inbound_flow import InboundFlow, inbound_flow
from llm_helpers import add_rules
from runtime_rig import WAIT_S

from va_domain.call_workflow import CallWorkflowInput
from va_edge.accept import ACCEPT_BUDGET_S
from va_media.session import STREAM_CLOSED, SessionEnd

pytestmark = pytest.mark.integration

PROMPT: Final = "You are the front desk of a dental clinic. Help callers book a visit."
OPENER: Final = "Thanks for calling the clinic, how can I help?"
REPLY: Final = "Sure, what day works for you?"
MAINTENANCE: Final = "Our lines are closed for maintenance today. Please call back tomorrow."


async def finished(flow: InboundFlow) -> list[tuple[str, str]]:
    """The media worker's ``CallWorkflow`` signal: ``(end_reason, ended_by)``."""
    async with asyncio.timeout(WAIT_S):
        await flow.runtime_signals.signalled.wait()
    return [(f.end_reason, f.ended_by) for _, f in flow.runtime_signals.signals]


def commands(flow: InboundFlow, ccid: str) -> list[str]:
    """The Call Control commands FakeTelnyx received for the leg, in order."""
    found = flow.state.telnyx.call(ccid)
    assert found is not None
    return [c.name for c in found[1].commands]


async def test_an_inbound_call_converses_and_ends_completed(media_db_url: str) -> None:
    async with inbound_flow(media_db_url) as flow:
        await add_rules(
            flow.llm,
            "rules:\n  - id: book\n    match: {last_user_regex: 'appointment'}\n"
            f"    respond: {{content: '{REPLY}'}}\n",
        )
        org = await flow.org()
        to = await flow.number(org, {"prompt": PROMPT, "first_sentence": OPENER})
        scenario = flow.caller(
            "wait_agent_speech: {}",
            "say: {text: I would like to make an appointment}",
            "wait_agent_speech: {}",
            "hangup: {}",
        )
        ccid = await flow.ring(to, scenario)
        assert await flow.body.end() == STREAM_CLOSED
        assert await finished(flow) == [("user_hangup", "user")]
        sent = commands(flow, ccid)
        call = await flow.call(org)
        turns = await flow.turns(org, call.id)
        answer_count, answer_total = flow.answer_ms()
    assert (call.direction, call.status, call.end_reason, call.ended_by) == (
        "inbound",
        "completed",
        "user_hangup",
        "user",
    )
    assert call.answered_at is not None
    # The edge answered (streaming to the media worker, which ran this call's turns below); nobody but the
    # caller hung up.
    assert sent == ["answer"]
    [started] = flow.temporal.started
    workflow_input = CallWorkflowInput.model_validate(started[1])
    assert (workflow_input.call_id, workflow_input.direction) == (call.id, "inbound")
    # The opener, the caller's line and the reply, in order.
    assert [(t.turn_index, t.role) for t in turns] == [(0, "assistant"), (1, "user"), (2, "assistant")]
    assert turns[0].text_ == OPENER
    assert "appointment" in turns[1].text_.lower()
    assert turns[2].text_ == REPLY
    assert [request.rule_id for request in flow.state.llm.requests] == ["book"]
    # Ring to answer, once, within the accept's answer budget.
    assert answer_count == 1
    assert answer_total <= ACCEPT_BUDGET_S * 1000


async def test_an_inbound_maintenance_call_hears_the_message_and_ends_inbound_rejected(
    media_db_url: str,
) -> None:
    async with inbound_flow(media_db_url) as flow:
        org = await flow.org()
        config = {"prompt": PROMPT, "maintenance": {"enabled": True, "message": MAINTENANCE}}
        to = await flow.number(org, config)
        ccid = await flow.ring(to, flow.caller("expect_hangup: {}"))
        assert await flow.body.end() == SessionEnd("ended", "inbound_rejected", "system")
        assert await finished(flow) == [("inbound_rejected", "system")]
        sent = commands(flow, ccid)
        call = await flow.call(org)
    assert (call.direction, call.status, call.end_reason, call.ended_by) == (
        "inbound",
        "completed",
        "inbound_rejected",
        "system",
    )
    assert sent == ["answer", "hangup"]
    generated = [r.generated_text.strip() for r in flow.state.elevenlabs.tts_requests]
    assert [text for text in generated if text] == [MAINTENANCE]
    assert flow.state.llm.requests == []
