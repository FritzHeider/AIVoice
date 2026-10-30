"""Call control API: inbound calls, first command timeout, summaries, timeline, remote hangup, reset."""

from __future__ import annotations

from telnyx_helpers import (
    APP_URL,
    CALLEE,
    INBOUND_NUMBER,
    RecordingTransport,
    answered_call,
    at,
    create_app,
    dial,
    events,
    inbound_call,
    ok,
    own_number,
    payloads,
    ringing_inbound,
    settle,
    summary,
    telnyx_client,
    telnyx_state,
)

from va_sim.scenario import load_scenario

INBOUND_YAML = f'id: caller\nmatch: {{inbound_from: "{CALLEE}"}}\nsteps:\n  - hangup: {{}}\n'


async def test_inbound_call_reaches_the_app_and_binds_the_scenario() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    state.register_scenario(load_scenario(INBOUND_YAML))
    async with telnyx_client(state) as client:
        app = await create_app(client)
        ids = await inbound_call(client, state, app["id"])
        await settle(state)
        [initiated] = transport.of_type("call.initiated")
        assert initiated.url == APP_URL
        payload = initiated.envelope["data"]["payload"]
        assert (payload["direction"], payload["state"], payload["to"], payload["from"]) == (
            "incoming",
            "parked",
            INBOUND_NUMBER,
            CALLEE,
        )
        assert payload["call_session_id"] == ids["call_session_id"]
        detail = await summary(client, ids["call_control_id"])
        assert (detail["scenario_id"], detail["direction"], detail["is_alive"]) == (
            "caller",
            "incoming",
            False,
        )


async def test_inbound_errors() -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        unowned = await client.post("/sim/telnyx/inbound", json={"to": INBOUND_NUMBER, "from": CALLEE})
        assert unowned.status_code == 422
        assert unowned.json()["error"]["code"] == "invalid_request"
        number = await own_number(client, state, INBOUND_NUMBER)
        no_connection = await client.post("/sim/telnyx/inbound", json={"to": INBOUND_NUMBER, "from": CALLEE})
        assert no_connection.status_code == 422
        app = await create_app(client)
        await client.patch(f"/v2/phone_numbers/{number['id']}", json={"connection_id": app["id"]})
        unknown = await client.post(
            "/sim/telnyx/inbound", json={"to": INBOUND_NUMBER, "from": CALLEE, "scenario": "nope"}
        )
        assert unknown.status_code == 404
        assert unknown.json()["error"]["code"] == "not_found"
        bad = await client.post("/sim/telnyx/inbound", json={"to": INBOUND_NUMBER})
        assert bad.status_code == 422


async def test_named_scenario_and_first_command_timeout() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    state.register_scenario(load_scenario(INBOUND_YAML.replace("caller", "named")))
    async with telnyx_client(state) as client:
        app = await create_app(client, first_command_timeout_secs=10)
        ids = await inbound_call(client, state, app["id"], scenario="named", **{"from": "+14155550177"})
        await state.tasks.drain()
        # The number order completes at 1 s, so the call starts there.
        assert events(transport) == [("call.initiated", 1000), ("call.hangup", 11_000)]
        [hangup] = payloads(transport, "call.hangup")
        assert (hangup["hangup_cause"], hangup["hangup_source"]) == ("timeout", "unknown")
        assert (await summary(client, ids["call_control_id"]))["scenario_id"] == "named"


async def test_a_command_in_time_disarms_the_first_command_timeout() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        app = await create_app(client, first_command_timeout_secs=10)
        ccid = (await inbound_call(client, state, app["id"]))["call_control_id"]
        await ok(client, ccid, "answer")
        await at(state, 20.0)
        assert payloads(transport, "call.hangup") == []


async def test_no_first_command_timeout_when_disabled() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        app = await create_app(client, first_command_timeout=False)
        await inbound_call(client, state, app["id"])
        await state.tasks.drain()
        assert events(transport) == [("call.initiated", 1000)]


async def test_listing_detail_and_timeline() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        app, ccid = await answered_call(client, state)
        await ok(client, ccid, "send_dtmf", digits="5", command_id="dtmf-1")
        await ok(client, ccid, "transfer", to="+14155550199")
        await at(state, 2.0)
        await ok(client, ccid, "hangup")
        await settle(state)
        listed = (await client.get("/sim/telnyx/calls")).json()["calls"]
        assert len(listed) == 2
        assert listed[0]["call_control_id"] == ccid
        detail = await summary(client, ccid)
        assert [c["name"] for c in detail["commands"]] == ["dial", "send_dtmf", "transfer", "hangup"]
        assert [e["event_type"] for e in detail["events"]][:2] == ["call.initiated", "call.answered"]
        assert detail["events"][1]["at"] == "2026-01-01T00:00:01Z"
        assert detail["connection_id"] == app
        assert (detail["created_at"], detail["answered_at"], detail["ended_at"]) == (
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:01Z",
            "2026-01-01T00:00:02Z",
        )
        assert detail["transfers"][0]["to"] == "+14155550199"
        timeline = (await client.get(f"/sim/telnyx/calls/{ccid}/timeline")).json()
        assert timeline == {
            "call_control_id": ccid,
            "initiated_ms": 0,
            "answered_ms": 1000,
            "ended_ms": 2000,
            "utterances": [],
            "script": None,
        }
        assert detail["script"] is None
        assert (await client.get("/sim/telnyx/calls/v3:none")).status_code == 404
        assert (await client.get("/sim/telnyx/calls/v3:none/timeline")).status_code == 404


async def test_remote_hangup() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        _, ccid = await answered_call(client, state)
        response = await client.post(f"/sim/telnyx/calls/{ccid}/hangup")
        assert response.status_code == 202
        assert (response.json()["state"], response.json()["hangup_source"]) == ("hangup", "callee")
        again = await client.post(f"/sim/telnyx/calls/{ccid}/hangup", json={"cause": "user_busy"})
        assert (again.status_code, again.json()["error"]["code"]) == (409, "call_ended")
        missing = await client.post("/sim/telnyx/calls/v3:none/hangup")
        assert missing.status_code == 404
        app = await create_app(client)
        ringing = (await dial(client, app["id"]))["call_control_id"]
        busy = await client.post(f"/sim/telnyx/calls/{ringing}/hangup", json={"cause": "not_found"})
        assert busy.json()["hangup_cause"] == "not_found"
        await settle(state)
        assert payloads(transport, "call.hangup")[-1]["sip_hangup_cause"] == "404"
        invalid = await client.post(f"/sim/telnyx/calls/{ringing}/hangup", json={"cause": "nope"})
        assert invalid.status_code == 422


async def test_remote_hangup_of_inbound_leg_is_caller_side() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        ccid = await ringing_inbound(client, state)
        await ok(client, ccid, "answer")
        response = await client.post(f"/sim/telnyx/calls/{ccid}/hangup")
        assert response.json()["hangup_source"] == "caller"


async def test_reset_cancels_progress_and_drops_calls() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        app = await create_app(client)
        await dial(client, app["id"])
        conference_call = await answered_call(client, state)
        await client.post("/v2/conferences", json={"call_control_id": conference_call[1], "name": "c"})
        snapshot = (await client.get("/sim/state")).json()["telnyx"]
        assert (snapshot["calls"], snapshot["conferences"]) == (2, 1)
        assert (await client.post("/sim/reset")).status_code == 204
        await settle(state)
        assert state.tasks.pending == 0
        assert (await client.get("/sim/telnyx/calls")).json() == {"calls": []}
        snapshot = (await client.get("/sim/state")).json()["telnyx"]
        assert (snapshot["calls"], snapshot["conferences"]) == (0, 0)
