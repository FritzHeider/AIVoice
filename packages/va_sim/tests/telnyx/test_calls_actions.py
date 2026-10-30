"""Call commands: answer/reject/hangup, errors, command_id dedup, client_state, DTMF, streaming, playback."""

from __future__ import annotations

import pytest
from telnyx_helpers import (
    APP_URL,
    RecordingTransport,
    answered_call,
    at,
    command,
    create_app,
    error_of,
    events,
    inbound_call,
    ok,
    payloads,
    ringing_call,
    ringing_inbound,
    settle,
    summary,
    telnyx_client,
    telnyx_state,
)
from telnyx_shapes import CallEventPayload, HangupPayload, assert_shape

from va_sim.telnyx.store import DEFAULT_API_KEY

STREAM_URL = "wss://media.sim/ws/telnyx?call_id=1"
HOLD_URL = "https://api.sim/static/hold/hold_music.wav"
OTHER_KEY = "other-account-key"


async def test_answer_inbound_leg() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        app = await create_app(client)
        ids = await inbound_call(client, state, app["id"])
        ccid = ids["call_control_id"]
        await settle(state)
        [initiated] = payloads(transport, "call.initiated")
        parsed = assert_shape(CallEventPayload, initiated)
        assert (parsed.direction, parsed.state, parsed.connection_id) == ("incoming", "parked", app["id"])
        await ok(client, ccid, "answer", client_state="YW5zd2Vy", stream_url=STREAM_URL)
        await settle(state)
        assert [event for event, _ in events(transport, ccid)] == [
            "call.initiated",
            "call.answered",
            "streaming.started",
        ]
        [answered] = payloads(transport, "call.answered")
        assert (answered["state"], answered["client_state"]) == ("answered", "YW5zd2Vy")
        assert payloads(transport, "streaming.started")[0]["stream_url"] == STREAM_URL
        detail = await summary(client, ccid)
        assert detail["stream"]["stream_url"] == STREAM_URL
        assert detail["stream"]["stream_track"] == "inbound_track"
        answer_again = await command(client, ccid, "answer")
        assert error_of(answer_again)[:2] == (422, "10015")


@pytest.mark.parametrize(
    ("cause", "expected"), [("USER_BUSY", "user_busy"), ("CALL_REJECTED", "call_rejected")]
)
async def test_reject_inbound_leg(cause: str, expected: str) -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        ccid = await ringing_inbound(client, state)
        await ok(client, ccid, "reject", cause=cause)
        await settle(state)
        [hangup] = payloads(transport, "call.hangup")
        assert (hangup["hangup_cause"], hangup["hangup_source"]) == (expected, "callee")


async def test_answer_and_reject_need_a_ringing_inbound_leg() -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        ccid = await ringing_call(client)
        for action, body in (("answer", {}), ("reject", {"cause": "USER_BUSY"})):
            response = await command(client, ccid, action, **body)
            assert error_of(response) == (422, "10015", None)
            assert "accepts" in response.json()["errors"][0]["detail"]


async def test_hangup_before_answer_is_originator_cancel() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        ccid = await ringing_call(client)
        await ok(client, ccid, "hangup")
        await state.tasks.drain()
        assert events(transport) == [("call.initiated", 0), ("call.hangup", 0)]
        hangup = assert_shape(HangupPayload, payloads(transport, "call.hangup")[0])
        assert (hangup.hangup_cause, hangup.sip_hangup_cause, hangup.hangup_source) == (
            "originator_cancel",
            "487",
            "caller",
        )


async def test_hangup_after_answer_and_commands_after_hangup() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        _, ccid = await answered_call(client, state)
        await ok(client, ccid, "hangup")
        await settle(state)
        [hangup] = payloads(transport, "call.hangup")
        assert (hangup["hangup_cause"], hangup["hangup_source"]) == ("normal_clearing", "caller")
        assert error_of(await command(client, ccid, "hangup")) == (422, "90018", None)
        assert error_of(await command(client, ccid, "send_dtmf", digits="1")) == (422, "90018", None)


async def test_platform_hangup_of_inbound_leg_is_callee_side() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        ccid = await ringing_inbound(client, state)
        await ok(client, ccid, "answer")
        await ok(client, ccid, "hangup")
        await settle(state)
        assert payloads(transport, "call.hangup")[0]["hangup_source"] == "callee"


async def test_unknown_call_other_account_and_unknown_action() -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        _, ccid = await answered_call(client, state)
        assert error_of(await command(client, "v3:missing", "hangup")) == (404, "10005", None)
        assert error_of(await command(client, ccid, "teleport")) == (404, "10005", None)
        state.telnyx.account(OTHER_KEY)
        other = await client.post(
            f"/v2/calls/{ccid}/actions/hangup", json={}, headers={"authorization": f"Bearer {OTHER_KEY}"}
        )
        assert error_of(other) == (404, "10005", None)
        owner = state.telnyx.call(ccid)
        assert owner is not None
        assert owner[0].api_key == DEFAULT_API_KEY


async def test_commands_before_answer_are_invalid_state() -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        ccid = await ringing_call(client)
        for action, body in (
            ("record_start", {}),
            ("streaming_start", {"stream_url": STREAM_URL}),
            ("playback_start", {"audio_url": HOLD_URL}),
            ("send_dtmf", {"digits": "1"}),
            ("transfer", {"to": "+14155550199"}),
            ("bridge", {"call_control_id": ccid}),
        ):
            assert error_of(await command(client, ccid, action, **body)) == (422, "10015", None), action


async def test_command_id_dedup_runs_once_and_logs_the_duplicate() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        _, ccid = await answered_call(client, state)
        await ok(client, ccid, "playback_start", audio_url=HOLD_URL, command_id="play-1")
        await ok(client, ccid, "playback_start", audio_url=HOLD_URL, command_id="play-1")
        await ok(client, ccid, "hangup", command_id="hangup-1")
        await ok(client, ccid, "hangup", command_id="hangup-1")
        await settle(state)
        assert len(payloads(transport, "call.playback.started")) == 1
        assert len(payloads(transport, "call.hangup")) == 1
        commands = (await summary(client, ccid))["commands"]
        assert [(c["name"], c["command_id"], c["duplicate"]) for c in commands] == [
            ("dial", None, False),
            ("playback_start", "play-1", False),
            ("playback_start", "play-1", True),
            ("hangup", "hangup-1", False),
            ("hangup", "hangup-1", True),
        ]


async def test_client_state_is_replaced_by_commands() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        _, ccid = await answered_call(client, state, client_state="Zmlyc3Q=")
        await ok(client, ccid, "playback_start", audio_url=HOLD_URL, client_state="c2Vjb25k")
        await ok(client, ccid, "hangup")
        await settle(state)
        states = [item.envelope["data"]["payload"]["client_state"] for item in transport.received]
        assert states == ["Zmlyc3Q=", "Zmlyc3Q=", "c2Vjb25k", "c2Vjb25k", "c2Vjb25k"]


async def test_send_dtmf_is_logged_and_validated() -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        _, ccid = await answered_call(client, state)
        await ok(client, ccid, "send_dtmf", digits="12#w*", duration_millis=300)
        await ok(client, ccid, "send_dtmf", digits="9")
        sent = (await summary(client, ccid))["dtmf_sent"]
        assert [(d["digits"], d["duration_millis"]) for d in sent] == [("12#w*", 300), ("9", 250)]
        assert sent[0]["at"] == "2026-01-01T00:00:01Z"
        assert error_of(await command(client, ccid, "send_dtmf", digits="12x")) == (422, "10015", "/digits")
        too_long = await command(client, ccid, "send_dtmf", digits="1", duration_millis=900)
        assert error_of(too_long) == (422, "10015", "/duration_millis")


async def test_streaming_start_stop_and_errors() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        _, ccid = await answered_call(client, state)
        assert error_of(await command(client, ccid, "streaming_start")) == (422, "10015", "/stream_url")
        assert error_of(await command(client, ccid, "streaming_stop")) == (422, "10015", None)
        await ok(client, ccid, "streaming_start", stream_url=STREAM_URL, stream_bidirectional_mode="rtp")
        assert error_of(await command(client, ccid, "streaming_start", stream_url=STREAM_URL))[:2] == (
            422,
            "10015",
        )
        stream = (await summary(client, ccid))["stream"]
        assert stream["stream_bidirectional_mode"] == "rtp"
        wrong = await command(client, ccid, "streaming_stop", stream_id="not-it")
        assert error_of(wrong) == (422, "10015", "/stream_id")
        await ok(client, ccid, "streaming_stop", stream_id=stream["stream_id"])
        await settle(state)
        assert [event for event, _ in events(transport)][-2:] == ["streaming.started", "streaming.stopped"]
        assert (await summary(client, ccid))["stream"] is None


async def test_dial_stream_url_starts_streaming_on_answer() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        _, ccid = await answered_call(client, state, stream_url=STREAM_URL, stream_codec="PCMU")
        assert events(transport, ccid) == [
            ("call.initiated", 0),
            ("call.answered", 1000),
            ("streaming.started", 1000),
        ]
        assert (await summary(client, ccid))["stream"]["stream_codec"] == "PCMU"


async def test_playback_start_stop() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        _, ccid = await answered_call(client, state)
        await ok(client, ccid, "playback_stop")
        await ok(client, ccid, "playback_start", audio_url=HOLD_URL)
        await ok(client, ccid, "playback_start", audio_url=HOLD_URL, overlay=True)
        await ok(client, ccid, "playback_stop", stop="current", overlay=True)
        await ok(client, ccid, "playback_stop")
        await settle(state)
        started = payloads(transport, "call.playback.started")
        assert [(p["media_url"], p["overlay"]) for p in started] == [(HOLD_URL, False), (HOLD_URL, True)]
        ended = payloads(transport, "call.playback.ended")
        assert [(p["overlay"], p["status"]) for p in ended] == [(True, "cancelled"), (False, "cancelled")]


async def test_hangup_side_effects_are_ordered() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        _, ccid = await answered_call(client, state, stream_url=STREAM_URL)
        await ok(client, ccid, "playback_start", audio_url=HOLD_URL)
        await ok(client, ccid, "record_start")
        await at(state, 3.0)
        await ok(client, ccid, "hangup")
        await settle(state)
        assert [event for event, _ in events(transport, ccid)] == [
            "call.initiated",
            "call.answered",
            "streaming.started",
            "call.playback.started",
            "streaming.stopped",
            "call.playback.ended",
            "call.hangup",
            "call.recording.saved",
        ]
        assert payloads(transport, "call.playback.ended")[0]["status"] == "call_hangup"
        assert all(item.url == APP_URL for item in transport.received)
