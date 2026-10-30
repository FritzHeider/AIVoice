"""Conferences: create, join, leave, end on exit, participant hangup, errors."""

from __future__ import annotations

from typing import Any

import httpx
from telnyx_helpers import (
    RecordingTransport,
    answered_call,
    at,
    dial,
    error_of,
    ok,
    payloads,
    settle,
    summary,
    telnyx_client,
    telnyx_state,
)
from telnyx_shapes import Conference, ConferenceEventPayload, assert_shape, assert_shapes

from va_sim.state import SimState

TARGET = "+14155550199"


async def two_answered_legs(client: httpx.AsyncClient, state: SimState) -> tuple[str, str]:
    app, a = await answered_call(client, state)
    b = (await dial(client, app, to=TARGET))["call_control_id"]
    await at(state, 2.0)
    return a, b


async def create(client: httpx.AsyncClient, ccid: str, **body: Any) -> dict[str, Any]:
    response = await client.post("/v2/conferences", json={"call_control_id": ccid, "name": "va-call", **body})
    assert response.status_code == 200, response.text
    conference: dict[str, Any] = response.json()["data"]
    return conference


async def member(client: httpx.AsyncClient, conference_id: str, action: str, **body: Any) -> httpx.Response:
    return await client.post(f"/v2/conferences/{conference_id}/actions/{action}", json=body)


def conference_events(transport: RecordingTransport) -> list[tuple[str, str]]:
    return [
        (item.event_type, item.envelope["data"]["payload"]["call_control_id"])
        for item in transport.received
        if item.event_type.startswith("conference.")
    ]


async def test_create_join_leave_all_left() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        a, b = await two_answered_legs(client, state)
        conference = assert_shape(Conference, await create(client, a, client_state="Y29uZg=="))
        assert (conference.status, conference.region, conference.name) == ("in_progress", "sim", "va-call")
        assert conference.expires_at == "2026-01-02T00:00:02Z"
        fetched = await client.get(f"/v2/conferences/{conference.id}")
        assert fetched.json()["data"] == conference.model_dump()
        assert (
            await member(client, conference.id, "join", call_control_id=b, command_id="j1")
        ).status_code == 200
        assert (
            await member(client, conference.id, "join", call_control_id=b, command_id="j1")
        ).status_code == 200
        assert (await summary(client, b))["conference_id"] == conference.id
        assert (await member(client, conference.id, "leave", call_control_id=a)).status_code == 200
        assert (await member(client, conference.id, "leave", call_control_id=b)).status_code == 200
        await settle(state)
        assert conference_events(transport) == [
            ("conference.created", a),
            ("conference.participant.joined", a),
            ("conference.participant.joined", b),
            ("conference.participant.left", a),
            ("conference.participant.left", b),
            ("conference.ended", b),
        ]
        conference_payloads = [
            item.envelope["data"]["payload"]
            for item in transport.received
            if item.event_type.startswith("conf")
        ]
        assert {p.conference_id for p in assert_shapes(ConferenceEventPayload, conference_payloads)} == {
            conference.id
        }
        assert payloads(transport, "conference.created")[0]["client_state"] == "Y29uZg=="
        assert payloads(transport, "conference.ended")[0]["reason"] == "all_left"
        ended = await client.get(f"/v2/conferences/{conference.id}")
        assert ended.json()["data"]["status"] == "completed"
        rejoin = await member(client, conference.id, "join", call_control_id=a)
        assert error_of(rejoin) == (422, "10015", None)


async def test_host_leaving_with_end_on_exit_ends_the_conference() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        a, b = await two_answered_legs(client, state)
        conference = await create(client, a)
        joined = await member(
            client, conference["id"], "join", call_control_id=b, end_conference_on_exit=True
        )
        assert joined.status_code == 200
        await ok(client, b, "hangup")
        await settle(state)
        assert conference_events(transport)[2:] == [
            ("conference.participant.joined", b),
            ("conference.participant.left", b),
            ("conference.ended", b),
            ("conference.participant.left", a),
        ]
        assert payloads(transport, "conference.ended")[0]["reason"] == "host_left"
        assert (await summary(client, a))["conference_id"] is None
        assert (await summary(client, a))["is_alive"] is True


async def test_conference_errors() -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        a, b = await two_answered_legs(client, state)
        missing = await client.post("/v2/conferences", json={"call_control_id": "v3:none", "name": "x"})
        assert error_of(missing) == (422, "10015", "/call_control_id")
        assert error_of(await client.get("/v2/conferences/nope")) == (404, "10005", None)
        assert error_of(await member(client, "nope", "join", call_control_id=a)) == (404, "10005", None)
        conference = await create(client, a)
        again = await client.post("/v2/conferences", json={"call_control_id": a, "name": "y"})
        assert error_of(again) == (422, "10015", None)
        assert error_of(await member(client, conference["id"], "join", call_control_id=a)) == (
            422,
            "10015",
            None,
        )
        assert error_of(await member(client, conference["id"], "leave", call_control_id=b)) == (
            422,
            "10015",
            None,
        )
        await ok(client, b, "hangup")
        assert error_of(await member(client, conference["id"], "join", call_control_id=b)) == (
            422,
            "90018",
            None,
        )
        created_twice = await client.post(
            "/v2/conferences", json={"call_control_id": a, "name": "va-call", "command_id": "c1"}
        )
        assert error_of(created_twice) == (422, "10015", None)


async def test_conference_create_dedup_and_unanswered_leg() -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        app, a = await answered_call(client, state)
        first = await create(client, a, command_id="conf-1")
        assert await create(client, a, command_id="conf-1") == first
        ringing = (await dial(client, app, to=TARGET))["call_control_id"]
        response = await client.post("/v2/conferences", json={"call_control_id": ringing, "name": "z"})
        assert error_of(response) == (422, "10015", None)
        leave = await member(client, first["id"], "leave", call_control_id=a, command_id="l1")
        assert leave.status_code == 200
        assert (
            await member(client, first["id"], "leave", call_control_id=a, command_id="l1")
        ).status_code == 200
        names = [(c["name"], c["duplicate"]) for c in (await summary(client, a))["commands"]]
        assert names[-4:] == [
            ("conference_create", False),
            ("conference_create", True),
            ("conference_leave", False),
            ("conference_leave", True),
        ]
