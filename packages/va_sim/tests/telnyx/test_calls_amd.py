"""Answering machine detection events driven by the scenario's ``answered_by`` and ``beep_at_ms``."""

from __future__ import annotations

import pytest
from telnyx_helpers import (
    CALLEE,
    RecordingTransport,
    at,
    create_app,
    dial,
    events,
    ok,
    payloads,
    register,
    telnyx_client,
    telnyx_state,
)
from telnyx_shapes import AmdPayload, assert_shape


def machine_yaml(answered_by: str, beep_at_ms: int | None = None) -> str:
    beep = f", beep_at_ms: {beep_at_ms}" if beep_at_ms is not None else ""
    return (
        f'id: amd\nmatch: {{to: "{CALLEE}"}}\nanswer: {{after_ms: 1000, answered_by: {answered_by}{beep}}}\n'
    )


async def amd_events(mode: str, scenario: str) -> tuple[RecordingTransport, list[tuple[str, int]]]:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    register(state, scenario)
    async with telnyx_client(state) as client:
        app = await create_app(client)
        await dial(client, app["id"], answering_machine_detection=mode, time_limit_secs=30)
        await state.tasks.drain()
    return transport, [(event, ms) for event, ms in events(transport) if "machine" in event]


@pytest.mark.parametrize("answered_by", ["human_residence", "human_business", "silence", "fax_detected"])
async def test_premium_reports_answered_by(answered_by: str) -> None:
    transport, seen = await amd_events("premium", machine_yaml(answered_by))
    assert seen == [("call.machine.premium.detection.ended", 2800)]
    [payload] = payloads(transport, "call.machine.premium.detection.ended")
    assert assert_shape(AmdPayload, payload).result == answered_by


@pytest.mark.parametrize(
    ("beep_at_ms", "expected"),
    [
        (
            3000,
            [("call.machine.premium.detection.ended", 2800), ("call.machine.premium.greeting.ended", 4000)],
        ),
        (
            500,
            [("call.machine.premium.greeting.ended", 1500), ("call.machine.premium.detection.ended", 2800)],
        ),
    ],
)
async def test_premium_machine_with_beep_in_time_order(
    beep_at_ms: int, expected: list[tuple[str, int]]
) -> None:
    transport, seen = await amd_events("premium", machine_yaml("machine", beep_at_ms))
    assert seen == expected
    assert payloads(transport, "call.machine.premium.greeting.ended")[0]["result"] == "beep_detected"
    assert payloads(transport, "call.machine.premium.detection.ended")[0]["result"] == "machine"


@pytest.mark.parametrize(
    ("answered_by", "result"),
    [
        ("human_residence", "human"),
        ("machine", "machine"),
        ("silence", "not_sure"),
        ("fax_detected", "not_sure"),
    ],
)
async def test_standard_detection_maps_results(answered_by: str, result: str) -> None:
    transport, seen = await amd_events("detect", machine_yaml(answered_by))
    assert seen == [("call.machine.detection.ended", 2800)]
    assert payloads(transport, "call.machine.detection.ended")[0]["result"] == result


@pytest.mark.parametrize(
    ("mode", "greeting"), [("detect_beep", True), ("greeting_end", True), ("detect_words", False)]
)
async def test_standard_greeting_events(mode: str, *, greeting: bool) -> None:
    _, seen = await amd_events(mode, machine_yaml("machine", 2000))
    expected = [("call.machine.detection.ended", 2800)]
    if greeting:
        expected.insert(0, ("call.machine.greeting.ended", 3000))
        expected.sort(key=lambda item: item[1])
    assert seen == expected


async def test_disabled_sends_no_detection_events() -> None:
    _, seen = await amd_events("disabled", machine_yaml("machine", 2000))
    assert seen == []


async def test_detection_is_cancelled_by_an_early_hangup() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    register(state, machine_yaml("machine", 5000))
    async with telnyx_client(state) as client:
        app = await create_app(client)
        ccid = (await dial(client, app["id"], answering_machine_detection="premium"))["call_control_id"]
        await at(state, 1.5)
        await ok(client, ccid, "hangup")
        await state.tasks.drain()
    assert [event for event, _ in events(transport)] == ["call.initiated", "call.answered", "call.hangup"]
