"""Contract tests: every call-control and conference method against FakeTelnyx (specs/06 §2.1; specs/30 §3.2).

FakeTelnyx forbids unknown body fields (``10015``), so a body the client builds wrongly fails here.
"""

from __future__ import annotations

from typing import Literal

import httpx
import pytest
from media_agent import MediaAgent
from telnyx_client_helpers import CALL_ID, leg_state, sim_clients
from telnyx_helpers import (
    CALLEE,
    CALLER,
    VOICE_URL,
    RecordingTransport,
    at,
    create_app,
    events,
    inbound_call,
    payloads,
    settle,
    summary,
    telnyx_state,
)

from va_sim.state import SimState
from va_telnyx import (
    AmdConfig,
    AnswerRequest,
    CallAlreadyEnded,
    CommandResult,
    CustomHeader,
    DialRequest,
    StreamParams,
    TelnyxApiError,
    TelnyxClient,
    TransferRequest,
    command_id,
)
from va_telnyx.client_state import decode

OK = CommandResult(result="ok")
AUTH = "stream-auth"
HOLD_URL = "https://api.sim/static/hold/hold_music.wav"


def _dial(connection_id: str, cid: str, **fields: object) -> DialRequest:
    values: dict[str, object] = {
        "connection_id": connection_id,
        "to": CALLEE,
        "from_": CALLER,
        "client_state": leg_state(),
        "command_id": cid,
        "webhook_url": VOICE_URL,
        **fields,
    }
    return DialRequest.model_validate(values)


async def _answered(client: TelnyxClient, connection_id: str, cid: str) -> str:
    result = await client.dial(_dial(connection_id, cid))
    return result.call_control_id


async def _inbound(raw: httpx.AsyncClient, state: SimState) -> str:
    """A ringing inbound call on a new Call Control App (its ``call.initiated`` delivered)."""
    app = await create_app(raw)
    ccid: str = (await inbound_call(raw, state, app["id"]))["call_control_id"]
    await settle(state)
    return ccid


async def test_dial_with_every_option_and_dedup_by_command_id() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with sim_clients(state) as (raw, client):
        app = await create_app(raw)
        request = _dial(
            app["id"],
            command_id("dial", CALL_ID, "primary"),
            from_display_name="Acme",
            timeout_secs=30,
            time_limit_secs=630,
            answering_machine_detection="premium",
            answering_machine_detection_config=AmdConfig(
                total_analysis_time_millis=5000, greeting_duration_millis=3500, silence_threshold=256
            ),
            record="record-from-answer",
            record_channels="dual",
            record_format="wav",
            record_track="both",
            custom_headers=[CustomHeader(name="X-VA-Call-Id", value=str(CALL_ID))],
        )
        result = await client.dial(request)
        again = await client.dial(request)
        assert again.call_control_id == result.call_control_id
        assert result.call_control_id.startswith("v3:")
        await at(state, 1.0)
        detail = await summary(raw, result.call_control_id)
        assert [(c["name"], c["duplicate"]) for c in detail["commands"]] == [("dial", False), ("dial", True)]
        assert (detail["from"], detail["to"], detail["state"]) == (CALLER, CALLEE, "answered")
        [initiated] = payloads(transport, "call.initiated")
        assert decode(initiated["client_state"]).call_id == CALL_ID
        assert {item.url for item in transport.received} == {VOICE_URL}


async def test_answer_with_stream_then_streaming_stop_and_hangup(media_agent: MediaAgent) -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with sim_clients(state) as (raw, client):
        ccid = await _inbound(raw, state)
        answer = AnswerRequest(
            client_state=leg_state(),
            command_id="answer-1",
            stream=StreamParams(stream_url=media_agent.url, stream_auth_token=AUTH),
            record="record-from-answer",
            record_channels="dual",
            record_format="wav",
        )
        assert await client.answer(ccid, answer) == OK
        await media_agent.wait_media(1)
        detail = await summary(raw, ccid)
        assert detail["stream"]["stream_url"] == media_agent.url
        assert detail["stream"]["stream_bidirectional_codec"] == "PCMU"
        assert detail["stream"]["stream_bidirectional_target_legs"] == "self"
        assert await client.streaming_stop(ccid, command_id="stop-1", client_state=leg_state()) == OK
        await media_agent.wait_closed()
        assert await client.hangup(ccid, command_id="hangup-1") is True
        await settle(state)
        assert [event for event, _ in events(transport, ccid)][:4] == [
            "call.initiated",
            "call.answered",
            "streaming.started",
            "streaming.stopped",
        ]
        assert decode(payloads(transport, "call.answered")[0]["client_state"]).leg == "primary"


async def test_streaming_start_on_an_answered_call(media_agent: MediaAgent) -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with sim_clients(state) as (raw, client):
        app = await create_app(raw)
        ccid = await _answered(client, app["id"], "dial-s")
        await at(state, 1.0)
        stream = StreamParams(
            stream_url=media_agent.url,
            stream_codec="L16",
            stream_bidirectional_codec="L16",
            stream_bidirectional_sampling_rate=16000,
        )
        assert (
            await client.streaming_start(ccid, stream, command_id="stream-1", client_state=leg_state()) == OK
        )
        await media_agent.wait_media(1)
        assert (await summary(raw, ccid))["stream"]["stream_bidirectional_sampling_rate"] == 16000
        assert await client.hangup(ccid, command_id="hangup-s")
        await media_agent.wait_closed()


@pytest.mark.parametrize(
    ("cause", "expected"), [("USER_BUSY", "user_busy"), ("CALL_REJECTED", "call_rejected")]
)
async def test_reject_inbound(cause: Literal["CALL_REJECTED", "USER_BUSY"], expected: str) -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with sim_clients(state) as (raw, client):
        ccid = await _inbound(raw, state)
        await client.reject(ccid, cause, command_id="reject-1")
        await settle(state)
        assert payloads(transport, "call.hangup")[0]["hangup_cause"] == expected


async def test_hangup_twice_and_commands_on_ended_or_unknown_calls() -> None:
    state = telnyx_state(RecordingTransport())
    async with sim_clients(state) as (raw, client):
        app = await create_app(raw)
        ccid = await _answered(client, app["id"], "dial-h")
        await at(state, 1.0)
        assert await client.hangup(ccid, command_id="hangup-1", client_state=leg_state()) is True
        await settle(state)
        assert await client.hangup(ccid, command_id="hangup-2") is False
        with pytest.raises(CallAlreadyEnded) as ended:
            await client.record_start(ccid, command_id="rec-1", client_state=leg_state())
        assert (ended.value.status, ended.value.errors[0].code) == (422, "90018")
        with pytest.raises(CallAlreadyEnded) as unknown:
            await client.playback_stop("v3:unknown", command_id="pb")
        assert unknown.value.status == 404
        assert await client.hangup("v3:unknown", command_id="h") is False


async def test_transfer_send_dtmf_record_and_playback() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with sim_clients(state) as (raw, client):
        app = await create_app(raw)
        ccid = await _answered(client, app["id"], "dial-t")
        await at(state, 1.0)
        assert await client.send_dtmf(ccid, "12#", 100, command_id="dtmf-1") == OK
        assert await client.record_start(ccid, command_id="rec-1", client_state=leg_state()) == OK
        assert await client.playback_start(ccid, HOLD_URL, command_id="pb-1", client_state=leg_state()) == OK
        await at(state, 3.0)
        assert await client.playback_stop(ccid, command_id="pb-stop-1") == OK
        assert await client.record_stop(ccid, command_id="rec-stop-1", client_state=leg_state()) == OK
        transfer = TransferRequest(
            to="+14155550199",
            from_=CALLER,
            time_limit_secs=900,
            target_leg_client_state=leg_state("transfer_target"),
            client_state=leg_state(),
            command_id=command_id("transfer", CALL_ID, "primary", 1),
        )
        assert await client.transfer(ccid, transfer) == OK
        await at(state, 10.0)
        detail = await summary(raw, ccid)
        assert [d["digits"] for d in detail["dtmf_sent"]] == ["12#"]
        assert [(t["to"], t["from"]) for t in detail["transfers"]] == [("+14155550199", CALLER)]
        assert len(detail["recordings"]) == 1
        names = {event for event, _ in events(transport, ccid)}
        assert {"call.playback.started", "call.playback.ended", "call.recording.saved"} <= names
        [saved] = payloads(transport, "call.recording.saved")
        assert decode(saved["client_state"]).leg == "primary"


async def test_bridge_two_answered_calls() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with sim_clients(state) as (raw, client):
        app = await create_app(raw)
        first = await _answered(client, app["id"], "dial-a")
        second = await _answered(client, app["id"], "dial-b")
        await at(state, 1.0)
        assert await client.bridge(first, second, command_id="bridge-1", client_state=leg_state()) == OK
        await settle(state)
        assert len(payloads(transport, "call.bridged")) == 2


async def test_conference_create_join_leave() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with sim_clients(state) as (raw, client):
        app = await create_app(raw)
        primary = await _answered(client, app["id"], "dial-p")
        proxy = await _answered(client, app["id"], "dial-x")
        await at(state, 1.0)
        conference = await client.conference_create(
            primary, f"va-{CALL_ID}", command_id="conf-c", client_state=leg_state()
        )
        assert (conference.name, conference.status) == (f"va-{CALL_ID}", "in_progress")
        again = await client.conference_create(
            primary, f"va-{CALL_ID}", command_id="conf-c", client_state=leg_state()
        )
        assert again.id == conference.id
        assert (
            await client.conference_join(
                conference.id,
                proxy,
                end_conference_on_exit=False,
                command_id="conf-j",
                client_state=leg_state("warm_proxy"),
            )
            == OK
        )
        assert await client.conference_leave(conference.id, proxy, command_id="conf-l") == OK
        await settle(state)
        joined = payloads(transport, "conference.participant.joined")
        assert [p["call_control_id"] for p in joined] == [primary, proxy]
        assert [p["call_control_id"] for p in payloads(transport, "conference.participant.left")] == [proxy]
        with pytest.raises(TelnyxApiError) as missing:
            await client.conference_leave("no-such-conference", proxy, command_id="conf-l2")
        assert missing.value.status == 404
        assert not isinstance(missing.value, CallAlreadyEnded)
