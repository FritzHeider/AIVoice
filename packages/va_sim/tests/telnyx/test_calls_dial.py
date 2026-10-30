"""Dial: validation, response shape, scenario-driven progress with sim-clock timings, webhook targets."""

from __future__ import annotations

import pytest
from telnyx_helpers import (
    APP_URL,
    CALLEE,
    CALLER,
    RecordingTransport,
    at,
    create_app,
    dial,
    error_of,
    events,
    payloads,
    register,
    summary,
    telnyx_client,
    telnyx_state,
    verify_signature,
)
from telnyx_shapes import CallEventPayload, DialResponse, HangupPayload, assert_shape

CUSTOM_URL = "http://edge:8100/telnyx/voice/custom"


def outcome_yaml(answer: str) -> str:
    return f'id: far-end\nmatch: {{to: "{CALLEE}"}}\nanswer: {answer}\n'


async def test_dial_requires_connection_to_and_from() -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        response = await client.post("/v2/calls", json={})
        assert response.status_code == 422
        pointers = {item["source"]["pointer"] for item in response.json()["errors"]}
        assert pointers == {"/connection_id", "/to", "/from"}
        assert {item["code"] for item in response.json()["errors"]} == {"10015"}


async def test_dial_rejects_unknown_connection_and_unknown_fields() -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        unknown = await client.post("/v2/calls", json={"connection_id": "404", "to": CALLEE, "from": CALLER})
        assert error_of(unknown) == (422, "10015", "/connection_id")
        app = await create_app(client)
        typo = await client.post(
            "/v2/calls", json={"connection_id": app["id"], "to": CALLEE, "from": CALLER, "time_limit": 60}
        )
        assert error_of(typo) == (422, "10015", "/time_limit")
        bad_to = await client.post(
            "/v2/calls", json={"connection_id": app["id"], "to": "555", "from": CALLER}
        )
        assert error_of(bad_to)[:2] == (422, "10015")
        missing_link = await client.post(
            "/v2/calls", json={"connection_id": app["id"], "to": CALLEE, "from": CALLER, "link_to": "v3:x"}
        )
        assert error_of(missing_link) == (422, "10015", "/link_to")


async def test_dial_response_and_default_progress() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        app = await create_app(client)
        call = assert_shape(DialResponse, await dial(client, app["id"], client_state="c3RhdGU="))
        assert (call.is_alive, call.client_state) == (False, "c3RhdGU=")
        await at(state, 1.0)
        assert events(transport, call.call_control_id) == [("call.initiated", 0), ("call.answered", 1000)]
        initiated, answered = (item.envelope["data"]["payload"] for item in transport.received)
        assert assert_shape(CallEventPayload, initiated).state == "bridging"
        payload = assert_shape(CallEventPayload, answered)
        assert (payload.state, payload.direction, payload.from_, payload.to) == (
            "answered",
            "outgoing",
            CALLER,
            CALLEE,
        )
        assert payload.client_state == "c3RhdGU="
        assert (payload.call_leg_id, payload.call_session_id) == (call.call_leg_id, call.call_session_id)
        assert all(item.url == APP_URL for item in transport.received)
        for item in transport.received:
            verify_signature(state.signing_key.public_key_b64, item.headers, item.body)
        detail = await summary(client, call.call_control_id)
        assert (detail["state"], detail["is_alive"], detail["scenario_id"]) == ("answered", True, None)


@pytest.mark.parametrize(
    ("answer", "cause", "sip", "source", "at_ms"),
    [
        ("{result: busy}", "user_busy", "486", "callee", 500),
        ("{result: rejected}", "call_rejected", "603", "callee", 500),
        ("{result: failed}", "unspecified", "503", "unknown", 500),
        ("{result: no_answer}", "timeout", "487", "unknown", 30_000),
        ("{after_ms: 45000}", "timeout", "487", "unknown", 30_000),
    ],
)
async def test_unanswered_outcomes(answer: str, cause: str, sip: str, source: str, at_ms: int) -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    register(state, outcome_yaml(answer))
    async with telnyx_client(state) as client:
        app = await create_app(client)
        call = await dial(client, app["id"])
        await state.tasks.drain()
        assert events(transport) == [("call.initiated", 0), ("call.hangup", at_ms)]
        [hangup] = payloads(transport, "call.hangup")
        parsed = assert_shape(HangupPayload, hangup)
        assert (parsed.hangup_cause, parsed.sip_hangup_cause, parsed.hangup_source) == (cause, sip, source)
        assert parsed.state == "hangup"
        detail = await summary(client, call["call_control_id"])
        assert (detail["state"], detail["is_alive"], detail["scenario_id"]) == ("hangup", False, "far-end")
        assert (detail["hangup_cause"], detail["hangup_source"]) == (cause, source)


async def test_no_answer_waits_for_the_dial_timeout() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    register(state, outcome_yaml("{result: no_answer}"))
    async with telnyx_client(state) as client:
        app = await create_app(client)
        await dial(client, app["id"], timeout_secs=12)
        await state.tasks.drain()
        assert events(transport)[-1] == ("call.hangup", 12_000)


async def test_answered_scenario_and_time_limit() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    register(state, outcome_yaml("{after_ms: 2500}"))
    async with telnyx_client(state) as client:
        app = await create_app(client)
        await dial(client, app["id"], time_limit_secs=60)
        await state.tasks.drain()
        assert events(transport) == [("call.initiated", 0), ("call.answered", 2500), ("call.hangup", 62_500)]
        [hangup] = payloads(transport, "call.hangup")
        assert (hangup["hangup_cause"], hangup["hangup_source"]) == ("time_limit", "unknown")


async def test_webhook_url_precedence() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        app = await create_app(client)
        await dial(client, app["id"], webhook_url=CUSTOM_URL)
        await at(state, 0)
        assert [item.url for item in transport.received] == [CUSTOM_URL]


async def test_dial_command_id_dedup_returns_the_same_call() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        app = await create_app(client)
        first = await dial(client, app["id"], command_id="dial-1")
        second = await dial(client, app["id"], command_id="dial-1")
        assert second["call_control_id"] == first["call_control_id"]
        listed = (await client.get("/sim/telnyx/calls")).json()["calls"]
        assert len(listed) == 1
        assert [(c["name"], c["duplicate"]) for c in listed[0]["commands"]] == [
            ("dial", False),
            ("dial", True),
        ]
        await at(state, 0)
        assert events(transport) == [("call.initiated", 0)]


async def test_same_seed_gives_the_same_ids() -> None:
    async def ids(seed: int) -> dict[str, object]:
        state = telnyx_state(RecordingTransport(), seed=seed)
        async with telnyx_client(state) as client:
            app = await create_app(client)
            return await dial(client, app["id"])

    assert await ids(7) == await ids(7)
    assert (await ids(7))["call_control_id"] != (await ids(8))["call_control_id"]


async def test_sip_destination_is_accepted() -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        app = await create_app(client)
        call = await dial(client, app["id"], to="sip:agent@pbx.sim")
        assert (await summary(client, call["call_control_id"]))["to"] == "sip:agent@pbx.sim"
