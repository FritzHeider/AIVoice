"""``hangup_call`` against FakeTelnyx (specs/06 §6 Decision P05-T06b item 7)."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator

import httpx
import orjson
import pytest
from call_rows import SeededCall, seed_leg
from service_helpers import json_log_stream, log_events
from telephony_rig import CALLEE, CALLER, Handler, Rig, failing_factory, rig
from telnyx_helpers import settle
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from va_db.session import Database
from va_telnyx import ClientState, DialRequest
from va_telnyx.client_state import decode, encode
from va_workflows.activities.errors import HANGUP_REJECTED, TELNYX_UNAVAILABLE
from va_workflows.activities.telephony import TelephonyActivities
from va_workflows.activities.telephony_inputs import HangupInput, HangupResult


@pytest.fixture
async def r(database: Database) -> AsyncIterator[Rig]:
    async with rig(database, answer="{after_ms: 200}", realtime=True) as built:
        yield built


async def _hangup(
    activities: TelephonyActivities, call: SeededCall, reason: str = "fax_detected"
) -> HangupResult:
    request = HangupInput(call_id=call.call_id, org_id=call.org_id, reason=reason)
    return await ActivityEnvironment().run(activities.hangup_call, request)


async def _answered(r: Rig) -> tuple[SeededCall, str]:
    """An in-progress call whose primary leg is a live FakeTelnyx call (dialled without media)."""
    call = await r.seed("in_progress")
    result = await r.factory.platform.dial(
        DialRequest(
            connection_id=r.app_id,
            to=CALLEE,
            from_=CALLER,
            client_state=encode(ClientState(call_id=call.call_id, leg="primary", org_id=call.org_id)),
            command_id=f"test-dial-{call.call_id}",
        )
    )
    await seed_leg(r.database, call, result.call_control_id)
    r.sent.clear()
    return call, result.call_control_id


async def test_hangup_ends_the_carrier_call_and_a_retry_is_deduplicated(r: Rig) -> None:
    stream = json_log_stream()
    call, ccid = await _answered(r)

    assert await _hangup(r.telephony(), call) == HangupResult(sent=True, reason="sent")
    assert await _hangup(r.telephony(), call) == HangupResult(sent=True, reason="sent")
    await settle(r.state)

    sim = (await r.raw.get(f"/sim/telnyx/calls/{ccid}")).json()
    assert sim["state"] == "hangup"
    assert [(c["name"], c["duplicate"]) for c in sim["commands"] if c["name"] == "hangup"] == [
        ("hangup", False),
        ("hangup", True),
    ]
    bodies = [orjson.loads(request.content) for request in r.commands("hangup")]
    assert [body["command_id"] for body in bodies] == [f"hangup-{call.call_id}-primary"] * 2
    assert decode(bodies[0]["client_state"]).call_id == call.call_id
    logged = [line for line in log_events(stream) if line["event"] == "calls.hangup_sent"]
    assert [(line["reason"], line["result"]) for line in logged] == [("fax_detected", "sent")] * 2


async def test_a_call_the_far_end_already_hung_up_counts_as_ended(r: Rig) -> None:
    call, ccid = await _answered(r)
    assert (await r.raw.post(f"/sim/telnyx/calls/{ccid}/hangup", json={})).status_code == 202
    await settle(r.state)

    assert await _hangup(r.telephony(), call, "worker_crash") == HangupResult(
        sent=False, reason="already_ended"
    )


async def test_a_call_without_a_leg_has_nothing_to_hang_up(r: Rig) -> None:
    call = await r.seed("dialing")

    assert await _hangup(r.telephony(), call) == HangupResult(sent=False, reason="no_leg")
    assert r.commands("hangup") == []


def _status(status: int, code: str = "10015") -> Handler:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"errors": [{"code": code, "title": "Nope"}]})

    return handler


async def _unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("slow", request=request)


@pytest.mark.parametrize(
    ("handler", "error_type", "non_retryable"),
    [
        (_unreachable, TELNYX_UNAVAILABLE, False),
        (_status(429, "10011"), TELNYX_UNAVAILABLE, False),
        (_status(502, "http_502"), TELNYX_UNAVAILABLE, False),
        (_status(422), HANGUP_REJECTED, True),
    ],
    ids=["transport", "throttled", "server_error", "rejected"],
)
async def test_hangup_failures(r: Rig, handler: Handler, error_type: str, non_retryable: bool) -> None:
    call = await r.seed("in_progress")
    await seed_leg(r.database, call, f"v3:{secrets.token_hex(8)}")

    with pytest.raises(ApplicationError) as caught:
        await _hangup(r.telephony(telnyx=failing_factory(r, handler)), call)

    assert (caught.value.type, caught.value.non_retryable) == (error_type, non_retryable)


async def test_a_leg_of_an_unusable_byo_account_is_rejected(r: Rig) -> None:
    call = await r.seed("in_progress")
    account_id = await r.byo_account(call.org_id, None)
    await seed_leg(r.database, call, f"v3:{secrets.token_hex(8)}", telnyx_account_id=account_id)

    with pytest.raises(ApplicationError) as caught:
        await _hangup(r.telephony(), call)

    assert (caught.value.type, caught.value.non_retryable) == (HANGUP_REJECTED, True)
