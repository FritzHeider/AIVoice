"""Every call-control and conference method sends exactly the specs/06 §2.1 body (respx)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
import respx
from telnyx_client_helpers import API_KEY, CCID, body_of, leg_state, rig

from va_telnyx import (
    AmdConfig,
    AnswerRequest,
    CommandResult,
    CustomHeader,
    DialRequest,
    StreamParams,
    TelnyxClient,
    TransferRequest,
)
from va_telnyx.transport import Operation

STATE = leg_state()
OK = {"data": {"result": "ok"}}
STREAM_URL = "wss://media.test/ws/telnyx?call_id=1&token=abc"
AUTH = "abc"
STREAM = StreamParams(stream_url=STREAM_URL, stream_auth_token=AUTH)
STREAM_BODY = {
    "stream_url": STREAM_URL,
    "stream_track": "inbound_track",
    "stream_codec": "PCMU",
    "stream_bidirectional_mode": "rtp",
    "stream_bidirectional_codec": "PCMU",
    "stream_bidirectional_sampling_rate": 8000,
    "stream_auth_token": "abc",
}
DIAL_RESULT = {
    "data": {
        "record_type": "call",
        "call_control_id": CCID,
        "call_leg_id": "leg-1",
        "call_session_id": "session-1",
        "is_alive": False,
        "client_state": STATE,
        "future_field": {"ignored": True},
    }
}


def _sent(route: respx.Route) -> httpx.Request:
    request = route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert request.headers["accept"] == "application/json"
    return request


async def test_dial_minimal_body(telnyx_api: respx.MockRouter) -> None:
    route = telnyx_api.post("/calls").respond(200, json=DIAL_RESULT)
    async with httpx.AsyncClient() as http:
        result = await rig(http).client.dial(
            DialRequest(
                connection_id="app-1",
                to="+14155550101",
                from_="+14155552000",
                client_state=STATE,
                command_id="d1",
            )
        )
    assert (result.call_control_id, result.call_leg_id, result.call_session_id) == (
        CCID,
        "leg-1",
        "session-1",
    )
    assert not result.is_alive
    assert body_of(_sent(route)) == {
        "connection_id": "app-1",
        "to": "+14155550101",
        "from": "+14155552000",
        "timeout_secs": 45,
        "time_limit_secs": 14_400,
        "answering_machine_detection": "disabled",
        "client_state": STATE,
        "command_id": "d1",
        "webhook_url_method": "POST",
        "send_silence_when_idle": True,
    }


async def test_dial_full_body(telnyx_api: respx.MockRouter) -> None:
    route = telnyx_api.post("/calls").respond(200, json=DIAL_RESULT)
    request = DialRequest(
        connection_id="app-1",
        to="+14155550101",
        from_="+14155552000",
        from_display_name="Acme",
        timeout_secs=30,
        time_limit_secs=630,
        answering_machine_detection="premium",
        answering_machine_detection_config=AmdConfig(total_analysis_time_millis=5000, silence_threshold=256),
        client_state=STATE,
        command_id="dial-1",
        webhook_url="https://edge.test/telnyx/voice",
        stream=STREAM,
        record="record-from-answer",
        record_channels="dual",
        record_format="wav",
        record_track="both",
        record_max_length=600,
        custom_headers=[CustomHeader(name="X-VA-Call-Id", value="c1")],
        link_to="v3:primary",
        bridge_intent=True,
        bridge_on_answer=False,
    )
    async with httpx.AsyncClient() as http:
        await rig(http).client.dial(request)
    assert body_of(_sent(route)) == {
        "connection_id": "app-1",
        "to": "+14155550101",
        "from": "+14155552000",
        "from_display_name": "Acme",
        "timeout_secs": 30,
        "time_limit_secs": 630,
        "answering_machine_detection": "premium",
        "answering_machine_detection_config": {"total_analysis_time_millis": 5000, "silence_threshold": 256},
        "client_state": STATE,
        "command_id": "dial-1",
        "webhook_url": "https://edge.test/telnyx/voice",
        "webhook_url_method": "POST",
        **STREAM_BODY,
        "stream_bidirectional_target_legs": "self",
        "record": "record-from-answer",
        "record_channels": "dual",
        "record_format": "wav",
        "record_track": "both",
        "record_max_length": 600,
        "custom_headers": [{"name": "X-VA-Call-Id", "value": "c1"}],
        "link_to": "v3:primary",
        "bridge_intent": True,
        "bridge_on_answer": False,
        "send_silence_when_idle": True,
    }


def _answer(client: TelnyxClient) -> Awaitable[CommandResult]:
    request = AnswerRequest(
        client_state=STATE,
        command_id="answer-1",
        webhook_url="https://edge.test/telnyx/voice",
        stream=STREAM,
        record="record-from-answer",
        record_channels="dual",
        record_format="wav",
    )
    return client.answer(CCID, request)


def _transfer(client: TelnyxClient) -> Awaitable[CommandResult]:
    request = TransferRequest(
        to="+14155550199",
        from_="+14155552000",
        time_limit_secs=900,
        target_leg_client_state=leg_state("transfer_target"),
        client_state=STATE,
        command_id="transfer-1",
        webhook_url="https://edge.test/telnyx/voice",
    )
    return client.transfer(CCID, request)


type Invoke = Callable[[TelnyxClient], Awaitable[Any]]

COMMANDS: list[tuple[str, Invoke, dict[str, Any]]] = [
    (
        "answer",
        _answer,
        {
            "client_state": STATE,
            "command_id": "answer-1",
            "webhook_url": "https://edge.test/telnyx/voice",
            **STREAM_BODY,
            "stream_bidirectional_target_legs": "self",
            "record": "record-from-answer",
            "record_channels": "dual",
            "record_format": "wav",
            "send_silence_when_idle": True,
        },
    ),
    (
        "reject",
        lambda c: c.reject(CCID, "USER_BUSY", command_id="reject-1"),
        {"cause": "USER_BUSY", "command_id": "reject-1"},
    ),
    ("hangup", lambda c: c.hangup(CCID, command_id="hangup-1"), {"command_id": "hangup-1"}),
    (
        "hangup",
        lambda c: c.hangup(CCID, command_id="hangup-2", client_state=STATE),
        {"command_id": "hangup-2", "client_state": STATE},
    ),
    (
        "transfer",
        _transfer,
        {
            "to": "+14155550199",
            "from": "+14155552000",
            "timeout_secs": 30,
            "time_limit_secs": 900,
            "early_media": True,
            "target_leg_client_state": leg_state("transfer_target"),
            "client_state": STATE,
            "command_id": "transfer-1",
            "webhook_url": "https://edge.test/telnyx/voice",
            "answering_machine_detection": "disabled",
        },
    ),
    (
        "bridge",
        lambda c: c.bridge(CCID, "v3:other", command_id="bridge-1", client_state=STATE),
        {"call_control_id": "v3:other", "client_state": STATE, "command_id": "bridge-1"},
    ),
    (
        "bridge",
        lambda c: c.bridge(CCID, "v3:other", command_id="b2", client_state=STATE, park_after_unbridge="self"),
        {
            "call_control_id": "v3:other",
            "client_state": STATE,
            "command_id": "b2",
            "park_after_unbridge": "self",
        },
    ),
    (
        "send_dtmf",
        lambda c: c.send_dtmf(CCID, "12*#AwW", command_id="dtmf-1"),
        {"digits": "12*#AwW", "duration_millis": 250, "command_id": "dtmf-1"},
    ),
    (
        "record_start",
        lambda c: c.record_start(CCID, command_id="rec-1", client_state=STATE),
        {
            "format": "wav",
            "channels": "dual",
            "play_beep": False,
            "max_length": 0,
            "timeout_secs": 0,
            "command_id": "rec-1",
            "client_state": STATE,
        },
    ),
    (
        "record_stop",
        lambda c: c.record_stop(CCID, command_id="rec-stop-1", client_state=STATE),
        {"command_id": "rec-stop-1", "client_state": STATE},
    ),
    (
        "streaming_start",
        lambda c: c.streaming_start(CCID, STREAM, command_id="stream-1", client_state=STATE),
        {**STREAM_BODY, "command_id": "stream-1", "client_state": STATE},
    ),
    (
        "streaming_start",
        lambda c: c.streaming_start(
            CCID,
            StreamParams(
                stream_url=STREAM_URL,
                stream_bidirectional_mode=None,
                stream_bidirectional_codec=None,
                stream_bidirectional_sampling_rate=None,
            ),
            command_id="listen-1",
            client_state=STATE,
        ),
        {
            "stream_url": STREAM_URL,
            "stream_track": "inbound_track",
            "stream_codec": "PCMU",
            "command_id": "listen-1",
            "client_state": STATE,
        },
    ),
    (
        "streaming_stop",
        lambda c: c.streaming_stop(CCID, command_id="stop-1", client_state=STATE),
        {"command_id": "stop-1", "client_state": STATE},
    ),
    (
        "streaming_stop",
        lambda c: c.streaming_stop(CCID, command_id="stop-2", client_state=STATE, stream_id="s-1"),
        {"command_id": "stop-2", "client_state": STATE, "stream_id": "s-1"},
    ),
    (
        "playback_start",
        lambda c: c.playback_start(
            CCID, "https://api.test/static/hold/hold_music.wav", command_id="pb-1", client_state=STATE
        ),
        {
            "audio_url": "https://api.test/static/hold/hold_music.wav",
            "loop": "infinity",
            "overlay": False,
            "target_legs": "self",
            "command_id": "pb-1",
            "client_state": STATE,
        },
    ),
    (
        "playback_stop",
        lambda c: c.playback_stop(CCID, command_id="pb-stop-1"),
        {"stop": "all", "command_id": "pb-stop-1"},
    ),
]


@pytest.mark.parametrize(("action", "invoke", "expected"), COMMANDS)
async def test_call_command_bodies(
    telnyx_api: respx.MockRouter, action: str, invoke: Invoke, expected: dict[str, Any]
) -> None:
    route = telnyx_api.post(f"/calls/{CCID}/actions/{action}").respond(200, json=OK)
    async with httpx.AsyncClient() as http:
        result = await invoke(rig(http).client)
    assert result in (True, CommandResult(result="ok"))
    request = _sent(route)
    assert request.url.raw_path.decode() == f"/v2/calls/{CCID}/actions/{action}"
    assert body_of(request) == expected


async def test_conference_bodies(telnyx_api: respx.MockRouter) -> None:
    create = telnyx_api.post("/conferences").respond(
        200,
        json={
            "data": {"id": "conf-1", "name": "va-c1", "status": "in_progress", "record_type": "conference"}
        },
    )
    join = telnyx_api.post("/conferences/conf-1/actions/join").respond(200, json=OK)
    leave = telnyx_api.post("/conferences/conf-1/actions/leave").respond(200, json=OK)
    async with httpx.AsyncClient() as http:
        client = rig(http).client
        conference = await client.conference_create(CCID, "va-c1", command_id="conf-c", client_state=STATE)
        await client.conference_join(
            "conf-1", "v3:proxy", end_conference_on_exit=False, command_id="conf-j", client_state=STATE
        )
        await client.conference_leave("conf-1", "v3:proxy", command_id="conf-l")
    assert (conference.id, conference.name, conference.status) == ("conf-1", "va-c1", "in_progress")
    assert body_of(_sent(create)) == {
        "call_control_id": CCID,
        "name": "va-c1",
        "beep_enabled": "never",
        "client_state": STATE,
        "command_id": "conf-c",
        "start_conference_on_create": True,
    }
    assert body_of(_sent(join)) == {
        "call_control_id": "v3:proxy",
        "start_conference_on_enter": True,
        "end_conference_on_exit": False,
        "hold": False,
        "mute": False,
        "client_state": STATE,
        "command_id": "conf-j",
    }
    assert body_of(_sent(leave)) == {"call_control_id": "v3:proxy", "command_id": "conf-l"}


async def test_ids_are_quoted_into_one_path_segment(telnyx_api: respx.MockRouter) -> None:
    route = telnyx_api.post("/calls/v3:a%2Fb%3Fc/actions/hangup").respond(200, json=OK)
    async with httpx.AsyncClient() as http:
        assert await rig(http).client.hangup("v3:a/b?c", command_id="h")
    assert route.calls.last.request.url.raw_path == b"/v2/calls/v3:a%2Fb%3Fc/actions/hangup"


@pytest.mark.parametrize(("digits", "duration"), [("", 250), ("12x", 250), ("a", 250), ("1", 99), ("1", 501)])
async def test_send_dtmf_validates_before_any_request(
    telnyx_api: respx.MockRouter, digits: str, duration: int
) -> None:
    route = telnyx_api.post(f"/calls/{CCID}/actions/send_dtmf").respond(200, json=OK)
    async with httpx.AsyncClient() as http:
        with pytest.raises(ValueError, match=r"DTMF|duration_millis"):
            await rig(http).client.send_dtmf(CCID, digits, duration, command_id="d")
    assert not route.called


def test_request_models_forbid_unknown_fields_and_keep_their_fixed_values() -> None:
    with pytest.raises(ValueError, match="extra"):
        AnswerRequest.model_validate(
            {"client_state": STATE, "command_id": "a", "stream_track": "both_tracks"}
        )
    with pytest.raises(ValueError, match="send_silence_when_idle"):
        AnswerRequest.model_validate(
            {"client_state": STATE, "command_id": "a", "send_silence_when_idle": False}
        )
    with pytest.raises(ValueError, match="stream_url"):
        StreamParams(stream_url="https://not-a-websocket")


async def test_client_repr_hides_the_api_key() -> None:
    async with httpx.AsyncClient() as http:
        client = rig(http).client
        assert API_KEY not in repr(client)
        assert repr(client) == "TelnyxClient(base_url='https://api.telnyx.test/v2', account='platform')"
        assert client.account == "platform"


async def test_absolute_urls_are_sent_without_the_api_key() -> None:
    url = "https://recordings.test/r1.wav?X-Amz-Signature=abc"
    with respx.mock(assert_all_mocked=True) as router:
        route = router.get(url).respond(200, json={"ok": True})
        async with httpx.AsyncClient() as http:
            transport = rig(http).client._transport
            operation = Operation("download", "GET", None, idempotent=True)
            assert await transport.request(operation, url) == {"ok": True}
    assert "authorization" not in route.calls.last.request.headers
    assert API_KEY not in repr(transport)
    assert repr(transport) == "TelnyxTransport(base_url='https://api.telnyx.test/v2', account='platform')"
