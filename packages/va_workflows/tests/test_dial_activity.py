"""``dial`` against FakeTelnyx and Postgres (specs/06 §6 Decision P05-T06b)."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import httpx
import orjson
import pytest
from billing_rows import holds_of
from call_rows import SeededCall, load_call, seed_leg
from compliance_rows import add_dnc
from resolved_configs import resolved_config
from service_helpers import json_log_stream, log_events
from sqlalchemy import delete
from telephony_rig import (
    CALLEE,
    CALLER,
    MEDIA_HOST,
    Handler,
    Rig,
    broken_redis,
    call_event_rows,
    expected_dial_body,
    failing_factory,
    funded_org,
    rig,
)
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from va_billing import keys as billing_keys
from va_billing.limits import CallLimiter
from va_common.ids import new_id
from va_common.stream_tokens import verify_stream_token
from va_common.time import SystemClock
from va_db.call_states import TERMINAL_STATUSES
from va_db.models import CallLeg, CallStatus, TelnyxAccount
from va_db.repositories import CallLegRepo, CallRepo, CallTransitionError
from va_db.session import Database
from va_events import DomainEvent, EventPublisher, EventPublishError, EventType, stream_for
from va_events.keys import stream_key
from va_telnyx import TelnyxApiError
from va_telnyx.client_state import decode
from va_workflows.activities.errors import (
    CALL_NOT_FOUND,
    DISPATCH_UNAVAILABLE,
    EVENTS_UNAVAILABLE,
    TELNYX_UNAVAILABLE,
)
from va_workflows.activities.telephony import (
    ACCOUNT_UNAVAILABLE_MESSAGE,
    TelephonyActivities,
    carrier_rejection,
    destination_region,
    release_slot,
)
from va_workflows.activities.telephony_inputs import DialInput, DialOutcome, max_duration_seconds

MAX_DURATION_S = 300
"""``max_duration_min`` 5 of the ``01_task_basic`` configuration the rig seeds."""


def test_max_duration_seconds_rounds_up() -> None:
    assert (max_duration_seconds(5.0), max_duration_seconds(0.5), max_duration_seconds(1.001)) == (
        300,
        30,
        61,
    )


@pytest.fixture
async def r(database: Database) -> AsyncIterator[Rig]:
    async with rig(database) as built:
        yield built


async def _dial(
    activities: TelephonyActivities, call: SeededCall, *, final_attempt: bool = False
) -> DialOutcome:
    request = DialInput(call_id=call.call_id, org_id=call.org_id, final_attempt=final_attempt)
    return await ActivityEnvironment().run(activities.dial, request)


async def _sim_call(r: Rig, ccid: str) -> dict[str, Any]:
    response = await r.raw.get(f"/sim/telnyx/calls/{ccid}")
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


async def _events(r: Rig, call: SeededCall) -> list[tuple[str, str, dict[str, Any]]]:
    return [(row.category, row.level, row.data) for row in await call_event_rows(r.database, call)]


def _failing(r: Rig, handler: Handler) -> TelephonyActivities:
    return r.telephony(telnyx=failing_factory(r, handler))


def _error(status: int, code: str = "10015", title: str = "Invalid value") -> Handler:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"errors": [{"code": code, "title": title}]})

    return handler


async def _unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


async def test_a_queued_call_is_dialled_with_the_exact_body(r: Rig) -> None:
    call = await r.seed("queued")

    outcome = await _dial(r.telephony(), call)

    [body] = r.dials()
    ccid = outcome.call_control_id
    assert ccid is not None
    assert outcome == DialOutcome(
        result="dialing", status="dialing", call_control_id=ccid, max_duration_s=MAX_DURATION_S
    )
    url = body.pop("stream_url")
    state = decode(body.pop("client_state"))
    assert body == expected_dial_body(call.call_id, connection_id=r.app_id, to=CALLEE, from_=CALLER)
    assert (state.call_id, state.leg, state.org_id) == (call.call_id, "primary", call.org_id)
    prefix = f"ws://{MEDIA_HOST}/ws/telnyx?call_id={call.call_id}&token="
    assert url.startswith(prefix)
    claims = verify_stream_token(
        url.removeprefix(prefix), secrets=(r.settings.stream_secret,), now=r.clock.now(), call_id=call.call_id
    )
    assert timedelta(minutes=14) < claims.expires_at - r.clock.now() <= timedelta(minutes=15)

    sim = await _sim_call(r, ccid)
    assert (sim["from"], sim["to"]) == (CALLER, CALLEE)
    row = await load_call(r.database, call)
    assert row.status == "dialing"
    assert row.dial_started_at is not None
    async with r.database.tenant_session(call.org_id) as session:
        leg = await session.get_one(CallLeg, (await _leg_id(r, call)))
    assert (leg.role, leg.state, leg.call_leg_id, leg.call_session_id) == (
        "primary",
        "initiated",
        sim["call_leg_id"],
        sim["call_session_id"],
    )
    assert await _events(r, call) == [("call", "info", {"call_control_id": ccid})]
    assert await r.redis.xlen(stream_key(stream_for(EventType.CALL_DIALING))) == 1


async def _leg_id(r: Rig, call: SeededCall) -> uuid.UUID:
    async with r.database.tenant_session(call.org_id) as session:
        leg = await CallLegRepo(session, call.org_id).primary(call.call_id)
    assert leg is not None
    return leg.id


async def test_a_retry_after_the_leg_was_recorded_sends_nothing(r: Rig) -> None:
    call = await r.seed("queued")
    first = await _dial(r.telephony(), call)

    again = await _dial(r.telephony(), call)

    assert again == DialOutcome(
        result="already_dialed",
        status="dialing",
        call_control_id=first.call_control_id,
        max_duration_s=MAX_DURATION_S,
    )
    assert len(r.dials()) == 1


async def test_dialing_without_a_leg_resends_the_same_command_and_telnyx_deduplicates_it(r: Rig) -> None:
    call = await r.seed("queued")
    first = await _dial(r.telephony(), call)
    async with r.database.tenant_session(call.org_id) as session:
        await session.execute(delete(CallLeg).where(CallLeg.call_id == call.call_id))

    again = await _dial(r.telephony(), call)

    assert again == first
    assert [body["command_id"] for body in r.dials()] == [f"dial-{call.call_id}"] * 2
    assert first.call_control_id is not None
    sim = await _sim_call(r, first.call_control_id)
    assert [(c["name"], c["duplicate"]) for c in sim["commands"]] == [("dial", False), ("dial", True)]
    assert [category for category, _, _ in await _events(r, call)] == ["call", "call"]


@pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES))
async def test_a_terminal_call_is_skipped(r: Rig, status: CallStatus) -> None:
    call = await r.seed(status)

    assert await _dial(r.telephony(), call) == DialOutcome(result="skipped", status=status)
    assert r.dials() == []


@pytest.mark.parametrize("status", ["ringing", "in_progress", "transferring", "transferred"])
async def test_a_call_past_the_dial_is_already_dialled(r: Rig, status: CallStatus) -> None:
    call = await r.seed(status)
    await seed_leg(r.database, call, f"v3:{secrets.token_hex(8)}")
    leg_less = await r.seed(status)

    outcome = await _dial(r.telephony(), call)

    assert (outcome.result, outcome.status, outcome.max_duration_s) == (
        "already_dialed",
        status,
        MAX_DURATION_S,
    )
    assert outcome.call_control_id is not None
    assert await _dial(r.telephony(), leg_less) == DialOutcome(
        result="already_dialed", status=status, max_duration_s=MAX_DURATION_S
    )
    assert r.dials() == []


async def test_an_unknown_call_fails_without_retries(r: Rig) -> None:
    with pytest.raises(ApplicationError) as caught:
        await _dial(r.telephony(), SeededCall(org_id=new_id(), call_id=new_id()))

    assert (caught.value.type, caught.value.non_retryable) == (CALL_NOT_FOUND, True)


async def test_a_byo_call_dials_with_the_account_key_and_app(r: Rig) -> None:
    byo_key = f"KEY-byo-{secrets.token_hex(6)}"
    assert (await r.raw.post("/sim/telnyx/accounts", json={"api_key": byo_key})).status_code == 201
    org_id = await funded_org(r.database)
    account_id = await r.byo_account(org_id, byo_key)
    byo_client = await r.factory.for_account(account_id)
    profile = await byo_client.create_outbound_voice_profile("byo")
    app = await byo_client.create_call_control_app(
        "byo", "https://edge.test/v", outbound_voice_profile_id=profile.id
    )
    await _set_app(r, org_id, account_id, app.id)
    r.sent.clear()
    config = {**resolved_config(), "telnyx_account_id": str(account_id)}
    call = await r.seed("queued", org_id=org_id, config=config)

    outcome = await _dial(r.telephony(), call)

    assert outcome.result == "dialing"
    [request] = [item for item in r.sent if item.url.path == "/v2/calls"]
    assert request.headers["authorization"] == f"Bearer {byo_key}"
    assert orjson.loads(request.content)["connection_id"] == app.id
    async with r.database.tenant_session(call.org_id) as session:
        leg = await session.get_one(CallLeg, await _leg_id(r, call))
    assert leg.telnyx_account_id == account_id


async def _set_app(r: Rig, org_id: uuid.UUID, account_id: uuid.UUID, app_id: str | None) -> None:
    async with r.database.tenant_session(org_id) as session:
        account = await session.get_one(TelnyxAccount, account_id)
        account.call_control_app_id = app_id


@pytest.mark.parametrize(("key", "app_id"), [("KEY-unused", None), (None, "app-x")], ids=["no_app", "no_key"])
async def test_a_byo_account_that_cannot_dial_fails_the_call(
    r: Rig, key: str | None, app_id: str | None
) -> None:
    org_id = await funded_org(r.database)
    account_id = await r.byo_account(org_id, key, app_id=app_id)
    config = {**resolved_config(), "telnyx_account_id": str(account_id)}
    call = await r.seed("queued", org_id=org_id, config=config)

    outcome = await _dial(r.telephony(), call)

    assert outcome == DialOutcome(result="failed", status="failed", code="account_unavailable")
    row = await load_call(r.database, call)
    assert (row.status, row.end_reason, row.error_message) == (
        "failed",
        "carrier_error",
        ACCOUNT_UNAVAILABLE_MESSAGE,
    )
    assert r.dials() == []


async def test_a_scheduled_call_is_admitted_then_dialled(r: Rig) -> None:
    call = await r.seed("scheduled")

    outcome = await _dial(r.telephony(), call)

    assert outcome.result == "dialing"
    row = await load_call(r.database, call)
    [hold] = await holds_of(r.database, call.org_id)
    assert (row.status, row.queue_status, row.credit_hold_id) == ("dialing", "queued", hold.id)
    assert (hold.status, hold.reference_type, hold.reference_id) == ("held", "call", str(call.call_id))
    assert await r.limiter.active_calls(call.org_id) == 1


async def _cap_full(r: Rig) -> SeededCall:
    org_id = await funded_org(r.database, limits={"concurrency": 1})
    expiry = int((r.clock.now() + timedelta(hours=1)).timestamp() * 1000)
    await r.redis.zadd(billing_keys.concurrency_key(org_id), {str(new_id()): expiry})
    return await r.seed("scheduled", org_id=org_id)


async def test_a_full_concurrency_cap_requeues_without_taking_anything(r: Rig) -> None:
    call = await _cap_full(r)

    outcome = await _dial(r.telephony(), call)

    assert outcome == DialOutcome(result="requeue", status="scheduled", code="CONCURRENCY_CAP_REACHED")
    row = await load_call(r.database, call)
    assert (row.status, row.credit_hold_id) == ("scheduled", None)
    assert await holds_of(r.database, call.org_id) == []
    assert int(await r.redis.get(billing_keys.daily_key(call.org_id, r.clock.now())) or 0) == 0
    assert r.dials() == []


async def test_a_full_cap_on_the_final_attempt_blocks_the_call(r: Rig) -> None:
    call = await _cap_full(r)

    outcome = await _dial(r.telephony(), call, final_attempt=True)

    assert outcome == DialOutcome(result="blocked", status="failed", code="CONCURRENCY_CAP_REACHED")
    row = await load_call(r.database, call)
    assert (row.status, row.end_reason, row.ended_by, row.queue_status) == (
        "failed",
        "dispatch_blocked",
        "system",
        "call_error",
    )


async def test_a_dnc_listed_destination_blocks_a_scheduled_call(r: Rig) -> None:
    call = await r.seed("scheduled")
    await add_dnc(r.database, call.org_id, CALLEE)

    outcome = await _dial(r.telephony(), call)

    assert (outcome.result, outcome.status) == ("blocked", "failed")
    assert outcome.code is not None
    row = await load_call(r.database, call)
    assert row.error_message is not None
    assert row.ended_at is not None
    assert await _events(r, call) == [
        ("queue", "warning", {"reason": outcome.code, "message": row.error_message}),
        ("call", "info", {"end_reason": "dispatch_blocked", "ended_by": "system"}),
    ]
    assert r.dials() == []


async def test_insufficient_credits_block_a_scheduled_call(r: Rig) -> None:
    org_id = await funded_org(r.database, balance=0)
    call = await r.seed("scheduled", org_id=org_id)

    outcome = await _dial(r.telephony(), call)

    assert outcome == DialOutcome(result="blocked", status="failed", code="INSUFFICIENT_CREDITS")
    assert await r.limiter.active_calls(org_id) == 0
    assert int(await r.redis.get(billing_keys.daily_key(org_id, r.clock.now())) or 0) == 0


async def test_unavailable_limits_retry_the_admission(r: Rig) -> None:
    call = await r.seed("scheduled")
    activities = r.telephony(limiter=CallLimiter(broken_redis(), clock=r.clock))

    with pytest.raises(ApplicationError) as caught:
        await _dial(activities, call)

    assert (caught.value.type, caught.value.non_retryable) == (DISPATCH_UNAVAILABLE, False)
    assert (await load_call(r.database, call)).status == "scheduled"


async def test_a_rejected_dial_fails_the_call_and_gives_back_hold_and_slot(r: Rig) -> None:
    call = await r.seed("scheduled")
    activities = _failing(r, _error(422, "10015", "Invalid value for connection_id"))

    outcome = await _dial(activities, call)

    assert outcome == DialOutcome(result="failed", status="failed", code="10015")
    row = await load_call(r.database, call)
    assert (row.status, row.end_reason, row.ended_by, row.queue_status, row.error_message) == (
        "failed",
        "carrier_error",
        "error",
        "call_error",
        "Carrier rejected the dial: Invalid value for connection_id",
    )
    [hold] = await holds_of(r.database, call.org_id)
    assert hold.status == "released"
    assert await r.limiter.active_calls(call.org_id) == 0
    message = row.error_message
    assert [
        (row.category, row.level, row.message, row.data) for row in await call_event_rows(r.database, call)
    ] == [
        ("error", "error", message, {"code": "dial_failed", "detail": "10015"}),
        ("call", "info", "Call ended.", {"end_reason": "carrier_error", "ended_by": "error"}),
    ]


async def test_an_error_body_without_items_reads_unknown(r: Rig) -> None:
    async def bare(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"errors": [{"title": ""}]})

    call = await r.seed("queued")

    outcome = await _dial(_failing(r, bare), call)

    assert outcome == DialOutcome(result="failed", status="failed", code="unknown")
    assert (await load_call(r.database, call)).error_message == "Carrier rejected the dial: unknown"


async def test_an_unreachable_carrier_retries_with_the_call_still_dialing(r: Rig) -> None:
    call = await r.seed("queued")

    with pytest.raises(ApplicationError) as caught:
        await _dial(_failing(r, _unreachable), call)

    assert (caught.value.type, caught.value.non_retryable) == (TELNYX_UNAVAILABLE, False)
    assert (await load_call(r.database, call)).status == "dialing"


async def test_a_server_error_left_after_the_client_retries_fails_the_call(r: Rig) -> None:
    call = await r.seed("queued")

    outcome = await _dial(_failing(r, _error(503, "http_503", "Service Unavailable")), call)

    assert outcome == DialOutcome(result="failed", status="failed", code="http_503")


async def test_a_carrier_rejection_after_a_concurrent_stop_is_skipped(r: Rig) -> None:
    call = await r.seed("queued")

    async def stop_then_reject(_request: httpx.Request) -> httpx.Response:
        async with r.database.tenant_session(call.org_id) as session:
            await CallRepo(session, call.org_id).transition(call.call_id, "canceled", end_reason="api_stop")
        return httpx.Response(422, json={"errors": [{"code": "10015", "title": "Invalid"}]})

    outcome = await _dial(_failing(r, stop_then_reject), call)

    assert outcome == DialOutcome(result="skipped", status="canceled", code="10015")
    assert (await load_call(r.database, call)).end_reason == "api_stop"
    assert await call_event_rows(r.database, call) == []


async def test_unavailable_events_roll_back_the_leg_and_the_retry_records_it(r: Rig) -> None:
    call = await r.seed("queued")
    broken = r.telephony()
    broken._publisher = EventPublisher(broken_redis(), call_events=None)

    with pytest.raises(ApplicationError) as caught:
        await _dial(broken, call)

    assert (caught.value.type, caught.value.non_retryable) == (EVENTS_UNAVAILABLE, False)
    assert (await load_call(r.database, call)).status == "dialing"
    async with r.database.tenant_session(call.org_id) as session:
        assert await CallLegRepo(session, call.org_id).primary(call.call_id) is None
    outcome = await _dial(r.telephony(), call)
    assert outcome.result == "dialing"
    assert len({body["command_id"] for body in r.dials()}) == 1


def _race(monkeypatch: pytest.MonkeyPatch, target: CallStatus, current: CallStatus) -> None:
    """Make the transition to ``target`` lose against a concurrent change to ``current``."""
    original = CallRepo.transition

    async def raced(self: CallRepo, call_id: uuid.UUID, to: CallStatus, **values: Any) -> object:
        if to == target:
            raise CallTransitionError(call_id, to, current)
        return await original(self, call_id, to, **values)

    monkeypatch.setattr(CallRepo, "transition", raced)


async def test_a_stop_racing_the_admission_gives_the_counters_back(
    r: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    call = await r.seed("scheduled")
    _race(monkeypatch, "queued", "canceled")

    outcome = await _dial(r.telephony(), call)

    assert outcome == DialOutcome(result="skipped", status="canceled")
    assert await holds_of(r.database, call.org_id) == []
    assert await r.limiter.active_calls(call.org_id) == 0
    assert int(await r.redis.get(billing_keys.daily_key(call.org_id, r.clock.now())) or 0) == 0
    assert r.dials() == []


async def test_a_stop_racing_the_block_is_skipped(r: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    call = await r.seed("scheduled")
    await add_dnc(r.database, call.org_id, CALLEE)
    _race(monkeypatch, "failed", "canceled")

    outcome = await _dial(r.telephony(), call)

    assert (outcome.result, outcome.status) == ("skipped", "canceled")
    assert outcome.code is not None
    assert await call_event_rows(r.database, call) == []


class _NoBroadcast(EventPublisher):
    async def broadcast(self, event: DomainEvent) -> None:
        raise EventPublishError(event, "stream")


async def test_a_failed_broadcast_still_dials(r: Rig) -> None:
    stream = json_log_stream()
    call = await r.seed("queued")

    outcome = await _dial(r.telephony(publisher=_NoBroadcast(r.redis, call_events=None)), call)

    assert outcome.result == "dialing"
    assert len(await call_event_rows(r.database, call)) == 1
    assert [
        line["event_type"] for line in log_events(stream) if line["event"] == "calls.event_broadcast_failed"
    ] == ["call.dialing"]


@pytest.mark.parametrize(("e164", "region"), [("+14155550100", "US"), ("+80012345678", None), ("12", None)])
def test_destination_region(e164: str, region: str | None) -> None:
    assert destination_region(e164) == region


async def test_a_slot_release_failure_is_logged() -> None:
    stream = json_log_stream()

    await release_slot(CallLimiter(broken_redis(), clock=SystemClock()), new_id(), new_id())

    [line] = [line for line in log_events(stream) if line["event"] == "calls.counter_release_failed"]
    assert line["error"] == "ConnectionError"


def test_a_rejection_without_error_items_reads_unknown() -> None:
    failure = carrier_rejection(TelnyxApiError(500, (), "dial"))

    assert (failure.message, failure.detail) == ("Carrier rejected the dial: unknown", "unknown")
