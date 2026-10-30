"""Transfers, bridges, unbridging, and dial ``link_to`` / ``bridge_on_answer``."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from telnyx_helpers import (
    CALLEE,
    RecordingTransport,
    answered_call,
    at,
    command,
    create_app,
    dial,
    error_of,
    events,
    ok,
    payloads,
    register,
    settle,
    summary,
    telnyx_client,
    telnyx_state,
)

from va_sim.state import SimState

TARGET = "+14155550199"
TARGET_STATE = "eyJsZWciOiJ0cmFuc2Zlcl90YXJnZXQifQ=="


def transfer_yaml(target: str) -> str:
    return f'id: xfer\nmatch: {{to: "{CALLEE}"}}\nanswer: {{after_ms: 1000}}\ntransfer_target: {target}\n'


async def transferred(client: httpx.AsyncClient, state: SimState, **body: Any) -> str:
    """Dial the scenario callee, transfer it to :data:`TARGET` once answered; returns leg A."""
    app = await create_app(client)
    a: str = (await dial(client, app["id"]))["call_control_id"]
    await at(state, 1.0)
    await ok(client, a, "transfer", to=TARGET, **body)
    return a


async def second_leg(client: httpx.AsyncClient, state: SimState) -> tuple[str, str]:
    """Leg A answered at 1 s and leg B to :data:`TARGET` dialled then (answered at 2 s)."""
    app, a = await answered_call(client, state)
    b: str = (await dial(client, app, to=TARGET))["call_control_id"]
    return a, b


async def test_transfer_answered_bridges_both_legs() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    register(state, transfer_yaml("{answer_after_ms: 4000}"))
    async with telnyx_client(state) as client:
        a = await transferred(client, state, target_leg_client_state=TARGET_STATE)
        await at(state, 5.0)
        detail = await summary(client, a)
        [transfer] = detail["transfers"]
        b = transfer["call_control_id"]
        assert (transfer["to"], transfer["from"]) == (TARGET, "+14155552000")
        assert [(e, ms) for e, ms in events(transport) if ms >= 1000][1:] == [
            ("call.initiated", 1000),
            ("call.answered", 5000),
            ("call.bridged", 5000),
            ("call.bridged", 5000),
        ]
        bridged = payloads(transport, "call.bridged")
        assert [p["call_control_id"] for p in bridged] == [a, b]
        b_detail = await summary(client, b)
        assert (b_detail["client_state"], b_detail["call_session_id"]) == (
            TARGET_STATE,
            detail["call_session_id"],
        )
        assert (detail["state"], b_detail["state"]) == ("bridged", "bridged")
        await ok(client, b, "hangup")
        await settle(state)
        hangups = payloads(transport, "call.hangup")
        assert [(p["call_control_id"], p["hangup_source"]) for p in hangups] == [(b, "caller"), (a, "callee")]


@pytest.mark.parametrize(
    ("target", "cause", "at_ms"),
    [("{result: busy}", "user_busy", 1500), ("{result: no_answer}", "timeout", 11_000)],
)
async def test_failed_transfer_keeps_the_original_leg(target: str, cause: str, at_ms: int) -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    register(state, transfer_yaml(target))
    async with telnyx_client(state) as client:
        a = await transferred(client, state, timeout_secs=10)
        await at(state, 12.0)
        [hangup] = payloads(transport, "call.hangup")
        assert hangup["hangup_cause"] == cause
        assert hangup["call_control_id"] != a
        assert ("call.hangup", at_ms) in events(transport)
        assert (await summary(client, a))["is_alive"] is True


async def test_transfer_without_scenario_answers_after_one_second() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        _, a = await answered_call(client, state)
        await ok(client, a, "transfer", to=TARGET, **{"from": "+14155552049"})
        await at(state, 2.0)
        assert payloads(transport, "call.bridged")[0]["call_control_id"] == a
        b = (await summary(client, a))["transfers"][0]
        assert b["from"] == "+14155552049"


async def test_bridge_two_calls_and_errors() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        a, b = await second_leg(client, state)
        assert error_of(await command(client, a, "bridge", call_control_id=b)) == (422, "10015", None)
        assert error_of(await command(client, a, "bridge", call_control_id="v3:none")) == (
            422,
            "10015",
            "/call_control_id",
        )
        assert error_of(await command(client, a, "bridge", call_control_id=a)) == (422, "10015", None)
        await at(state, 2.0)
        await ok(client, a, "bridge", call_control_id=b)
        await settle(state)
        assert [p["call_control_id"] for p in payloads(transport, "call.bridged")] == [a, b]
        assert error_of(await command(client, a, "bridge", call_control_id=b)) == (422, "10015", None)


async def test_bridge_with_an_ended_call_is_call_ended() -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        a, b = await second_leg(client, state)
        await ok(client, b, "hangup")
        assert error_of(await command(client, a, "bridge", call_control_id=b)) == (422, "90018", None)


async def test_park_after_unbridge_keeps_the_commanding_leg() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        a, b = await second_leg(client, state)
        await at(state, 2.0)
        await ok(client, a, "bridge", call_control_id=b, park_after_unbridge="self")
        await ok(client, b, "hangup")
        await settle(state)
        assert len(payloads(transport, "call.hangup")) == 1
        detail = await summary(client, a)
        assert (detail["state"], detail["is_alive"]) == ("parked", True)


async def test_link_to_with_bridge_on_answer_uses_transfer_target() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    register(state, transfer_yaml("{answer_after_ms: 2000}"))
    async with telnyx_client(state) as client:
        app = await create_app(client)
        a = (await dial(client, app["id"]))["call_control_id"]
        await at(state, 1.0)
        proxy = await dial(client, app["id"], to=TARGET, link_to=a, bridge_intent=True, bridge_on_answer=True)
        await at(state, 3.0)
        assert ("call.answered", 3000) in events(transport, proxy["call_control_id"])
        bridged = [p["call_control_id"] for p in payloads(transport, "call.bridged")]
        assert bridged == [a, proxy["call_control_id"]]
        a_detail = await summary(client, a)
        assert proxy["call_session_id"] == a_detail["call_session_id"]
        assert (await summary(client, proxy["call_control_id"]))["scenario_id"] == "xfer"


async def test_link_to_without_bridge_on_answer_does_not_bridge() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        app, a = await answered_call(client, state)
        await dial(client, app, to=TARGET, link_to=a)
        await at(state, 2.5)
        assert payloads(transport, "call.bridged") == []


async def test_hang_up_during_transfer_ring_cancels_the_transfer_leg() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    register(state, transfer_yaml("{answer_after_ms: 4000}"))
    async with telnyx_client(state) as client:
        a = await transferred(client, state)
        b = (await summary(client, a))["transfers"][0]["call_control_id"]
        await at(state, 2.0)
        await ok(client, a, "hangup")
        await at(state, 10.0)
        hangups = payloads(transport, "call.hangup")
        assert [(p["call_control_id"], p["hangup_cause"], p["hangup_source"]) for p in hangups] == [
            (a, "normal_clearing", "caller"),
            (b, "originator_cancel", "caller"),
        ]
        assert hangups[1]["sip_hangup_cause"] == "487"
        assert ("call.hangup", 2000) in events(transport, b)
        assert "call.answered" not in [e for e, _ in events(transport, b)]
        b_detail = await summary(client, b)
        assert (b_detail["state"], b_detail["is_alive"]) == ("hangup", False)


async def test_hang_up_cancels_a_ringing_link_to_bridge_on_answer_leg() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        app, a = await answered_call(client, state)
        proxy = await dial(client, app, to=TARGET, link_to=a, bridge_on_answer=True)
        await ok(client, a, "hangup")
        await at(state, 5.0)
        [_, hangup] = payloads(transport, "call.hangup")
        assert (hangup["call_control_id"], hangup["hangup_cause"], hangup["hangup_source"]) == (
            proxy["call_control_id"],
            "originator_cancel",
            "caller",
        )
        assert payloads(transport, "call.bridged") == []


async def test_leg_answering_after_its_link_to_peer_ended_is_hung_up() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        app, a = await answered_call(client, state)
        await ok(client, a, "hangup")
        proxy = (await dial(client, app, to=TARGET, link_to=a, bridge_on_answer=True))["call_control_id"]
        await at(state, 5.0)
        assert [e for e, _ in events(transport, proxy)] == ["call.initiated", "call.answered", "call.hangup"]
        hangup = payloads(transport, "call.hangup")[-1]
        assert (hangup["hangup_cause"], hangup["hangup_source"]) == ("normal_clearing", "caller")
        assert payloads(transport, "call.bridged") == []
