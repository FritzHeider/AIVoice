"""A cold transfer end to end in one process (specs/06 §10.1 Decision P05-T13): FakeTelnyx rings a number,
the edge answers, the media worker's TaskBrain asks for ``transfer_call``, the media worker transfers the
caller, and the edge bills the bridged time (or the conversation resumes after a failed transfer). The docker
e2e harness of P05-T17 runs the same flow across containers (``cold_transfer``)."""

from __future__ import annotations

import asyncio
import secrets
from typing import Any, Final

import pytest
from inbound_flow import InboundFlow, inbound_flow
from llm_helpers import add_rules
from runtime_rig import WAIT_S
from sqlalchemy import select

from va_db.models import CallEvent
from va_media.session import STREAM_CLOSED, SessionEnd

pytestmark = pytest.mark.integration

PROMPT: Final = "You are the front desk of a dental clinic. Transfer callers who ask for a human."
OPENER: Final = "Thanks for calling the clinic, how can I help?"
CONNECTING: Final = "Connecting you now."
FAILED_MESSAGE: Final = "I'm sorry, I wasn't able to connect you. Is there anything else I can help with?"
GOODBYE: Final = "Alright, have a nice day."
CALL_S: Final = 60.0
"""Bound of a whole call (the failed transfer converses for about 25 s of wall time)."""
TRANSFER_RULE: Final = (
    "rules:\n"
    "  - id: transfer\n"
    "    match: {last_user_regex: 'human', call_index: 0}\n"
    f"    respond: {{tool_calls: [{{name: transfer_call, arguments: {{message: '{CONNECTING}'}}}}]}}\n"
    "  - id: goodbye\n"
    "    match: {last_user_regex: 'all'}\n"
    f"    respond: {{content: '{GOODBYE}'}}\n"
)


def target_number() -> str:
    return f"+1628555{secrets.randbelow(10_000):04d}"


async def finished(flow: InboundFlow) -> list[tuple[str, str]]:
    async with asyncio.timeout(WAIT_S):
        await flow.runtime_signals.signalled.wait()
    return [(f.end_reason, f.ended_by) for _, f in flow.runtime_signals.signals]


def primary_commands(flow: InboundFlow, ccid: str) -> list[str]:
    found = flow.state.telnyx.call(ccid)
    assert found is not None
    return [c.name for c in found[1].commands]


def hangup_of(ccid: str) -> Any:
    def seen(documents: list[dict[str, Any]]) -> bool:
        return any(
            d["event_type"] == "call.hangup" and d["payload"].get("call_control_id") == ccid
            for d in documents
        )

    return seen


async def settled(flow: InboundFlow, ccid: str) -> None:
    """Until the edge stored the primary leg's hangup and processed every queued webhook."""
    async with asyncio.timeout(WAIT_S):
        await flow.forwarder.wait_for(hangup_of(ccid))
        await flow.edge_app.state.processor.join()


async def transfer_events(flow: InboundFlow, call: Any) -> list[dict[str, Any]]:
    """``data`` of the call's ``transfer`` category rows in ``seq`` order (``call.transfer_started`` carries
    ``type``/``target``, the edge's ``call.transferred`` none)."""
    async with flow.database.tenant_session(call.org_id) as session:
        rows = await session.scalars(
            select(CallEvent.data)
            .where(
                CallEvent.org_id == call.org_id,
                CallEvent.call_id == call.id,
                CallEvent.category == "transfer",
            )
            .order_by(CallEvent.seq)
        )
        return list(rows.all())


def spoken(flow: InboundFlow) -> list[str]:
    return [r.generated_text.strip() for r in flow.state.elevenlabs.tts_requests if r.generated_text.strip()]


async def test_a_cold_transfer_bridges_and_bills_transfer_seconds(media_db_url: str) -> None:
    target = target_number()
    async with inbound_flow(media_db_url) as flow:
        await add_rules(flow.llm, TRANSFER_RULE)
        org = await flow.org()
        to = await flow.number(
            org, {"prompt": PROMPT, "first_sentence": OPENER, "transfer_phone_number": target}
        )
        scenario = flow.caller(
            "wait_agent_speech: {}",
            "say: {text: Can I talk to a human please}",
            "expect_hangup: {timeout_ms: 60000}",
            transfer_target="{answer_after_ms: 500}",
        )
        ccid = await flow.ring(to, scenario)
        assert await flow.body.end(CALL_S) == SessionEnd("ended", "transferred", "system")
        assert await finished(flow) == [("transferred", "system")]
        found = flow.state.telnyx.call(ccid)
        assert found is not None
        [transfer] = found[1].transfers
        # Caller and target talk without the AI; the caller hangs up later.
        response = await flow.sim.post(
            f"/v2/calls/{ccid}/actions/hangup", json={"command_id": f"test-hangup-{secrets.token_hex(4)}"}
        )
        assert response.status_code == 200, response.text
        await settled(flow, ccid)
        sent = primary_commands(flow, ccid)
        call = await flow.call(org)
        events = await transfer_events(flow, call)
    assert sent == ["answer", "transfer", "streaming_stop", "hangup"]
    assert transfer.to == target
    assert CONNECTING in spoken(flow)
    assert (call.status, call.end_reason, call.ended_by) == ("completed", "transferred", "system")
    assert call.transferred_to == target
    assert call.connected_seconds is not None
    assert call.connected_seconds > 0
    assert call.transfer_seconds is not None
    assert call.transfer_seconds > 0
    assert events == [{"type": "cold", "target": target}, {}]
    assert [request.rule_id for request in flow.state.llm.requests] == ["transfer"]


async def test_a_failed_cold_transfer_resumes_the_conversation(media_db_url: str) -> None:
    async with inbound_flow(media_db_url) as flow:
        await add_rules(flow.llm, TRANSFER_RULE)
        org = await flow.org()
        target = target_number()
        config = {"prompt": PROMPT, "first_sentence": OPENER, "transfer_phone_number": target}
        to = await flow.number(org, config)
        scenario = flow.caller(
            "wait_agent_speech: {}",
            "say: {text: Can I talk to a human please}",
            "wait_agent_speech: {then_silence_ms: 4000, timeout_ms: 30000}",
            "say: {text: No thanks that is all}",
            "wait_agent_speech: {}",
            "hangup: {}",
            transfer_target="{result: busy}",
        )
        ccid = await flow.ring(to, scenario)
        assert await flow.body.end(CALL_S) == STREAM_CLOSED
        assert await finished(flow) == [("user_hangup", "user")]
        await settled(flow, ccid)
        sent = primary_commands(flow, ccid)
        call = await flow.call(org)
        events = await transfer_events(flow, call)
    assert sent == ["answer", "transfer"]
    assert (call.status, call.end_reason, call.ended_by) == ("completed", "user_hangup", "user")
    assert call.transferred_to is None
    assert not call.transfer_seconds
    assert events == [{"type": "cold", "target": target}]
    assert spoken(flow)[-3:] == [CONNECTING, FAILED_MESSAGE, GOODBYE]
    rule_ids = [request.rule_id for request in flow.state.llm.requests]
    assert rule_ids == ["transfer", "goodbye"]
    last: list[Any] = flow.state.llm.requests[-1].messages
    tail = [(m["role"], m.get("content")) for m in last[-2:]]
    assert tail[0] == ("assistant", FAILED_MESSAGE)
    assert tail[1][0] == "user"
    assert "all" in str(tail[1][1]).lower()
