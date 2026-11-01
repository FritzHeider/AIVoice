"""``end_call``: workflow-decided call ends (specs/05 §8, §9; specs/06 §6 Decision P05-T06b item 5)."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from billing_rows import holds_of
from call_rows import SeededCall, load_call, seed_leg
from telephony_rig import Rig, broken_redis, call_event_rows, funded_org, rig
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from va_billing import holds
from va_billing import keys as billing_keys
from va_common.ids import new_id
from va_db.call_states import TERMINAL_STATUSES
from va_db.models import CallStatus
from va_db.repositories import CallRepo, CallTransitionError
from va_db.session import Database
from va_domain.carrier import carrier_state_key
from va_workflows.activities.call_end import CallEndActivities
from va_workflows.activities.errors import CALL_NOT_FOUND, INVALID_END_REASON, STATE_UNAVAILABLE
from va_workflows.activities.telephony_inputs import EndCallInput, EndCallResult, WorkflowEndReason


@pytest.fixture
async def r(database: Database) -> AsyncIterator[Rig]:
    async with rig(database) as built:
        yield built


async def _end(
    activities: CallEndActivities,
    call: SeededCall,
    reason: WorkflowEndReason,
    *,
    error_message: str | None = None,
    code: str | None = None,
) -> EndCallResult:
    request = EndCallInput(
        call_id=call.call_id, org_id=call.org_id, end_reason=reason, error_message=error_message, code=code
    )
    return await ActivityEnvironment().run(activities.end_call, request)


async def _held_call(r: Rig, status: CallStatus, **values: Any) -> SeededCall:
    """A call of a funded org holding a $1 credit hold and a concurrency slot."""
    org_id = await funded_org(r.database)
    async with r.database.tenant_session(org_id) as session:
        hold = await holds.reserve(
            session,
            org_id,
            1_000_000,
            reference_type="call",
            reference_id=str(new_id()),
            expires_at=r.clock.now() + timedelta(hours=1),
        )
        hold_id = hold.id
    call = await r.seed(status, org_id=org_id, credit_hold_id=hold_id, **values)
    expiry = int((r.clock.now() + timedelta(hours=1)).timestamp() * 1000)
    await r.redis.zadd(billing_keys.concurrency_key(org_id), {str(call.call_id): expiry})
    return call


async def _hold_status(r: Rig, call: SeededCall) -> str:
    [hold] = await holds_of(r.database, call.org_id)
    return hold.status


async def _slot_taken(r: Rig, call: SeededCall) -> bool:
    return await r.redis.zscore(billing_keys.concurrency_key(call.org_id), str(call.call_id)) is not None


@pytest.mark.parametrize(
    ("status", "with_leg", "released"),
    [
        ("scheduled", False, True),
        ("queued", False, True),
        ("dialing", False, True),
        ("dialing", True, False),
        ("ringing", True, False),
    ],
)
@pytest.mark.parametrize("reason", ["canceled", "error", "carrier_error"])
async def test_an_unanswered_call_ends_canceled_or_failed(
    r: Rig, status: CallStatus, with_leg: bool, released: bool, reason: WorkflowEndReason
) -> None:
    call = await _held_call(r, status)
    if with_leg:
        await seed_leg(r.database, call, f"v3:{secrets.token_hex(8)}")

    result = await _end(r.call_end(), call, reason, error_message="Stopped by the workflow.")

    expected: CallStatus = "canceled" if reason == "canceled" else "failed"
    assert result == EndCallResult(changed=True, status=expected)
    row = await load_call(r.database, call)
    assert (row.status, row.end_reason, row.error_message) == (expected, reason, "Stopped by the workflow.")
    assert row.ended_at is not None
    if reason == "canceled":
        assert (row.ended_by, row.disposition_tag, row.queue_status) == ("api", "CANCELED", None)
    else:
        assert (row.ended_by, row.disposition_tag, row.queue_status) == ("error", None, "call_error")
    assert await _hold_status(r, call) == ("released" if released else "held")
    assert not await _slot_taken(r, call)
    ended_by = "api" if reason == "canceled" else "error"
    assert [(e.category, e.data) for e in await call_event_rows(r.database, call)] == [
        ("call", {"end_reason": reason, "ended_by": ended_by})
    ]


async def test_dispatch_blocked_records_both_events_in_order(r: Rig) -> None:
    call = await _held_call(r, "scheduled")

    result = await _end(
        r.call_end(),
        call,
        "dispatch_blocked",
        error_message="Calling hours are closed.",
        code="CALLING_HOURS",
    )

    assert result == EndCallResult(changed=True, status="failed")
    rows = await call_event_rows(r.database, call)
    assert [(e.category, e.level, e.message, e.data) for e in rows] == [
        (
            "queue",
            "warning",
            "Calling hours are closed.",
            {"reason": "CALLING_HOURS", "message": "Calling hours are closed."},
        ),
        ("call", "info", "Call ended.", {"end_reason": "dispatch_blocked", "ended_by": "system"}),
    ]


async def test_dispatch_blocked_without_details_uses_the_defaults(r: Rig) -> None:
    call = await r.seed("queued")

    await _end(r.call_end(), call, "dispatch_blocked")

    [blocked, _ended] = await call_event_rows(r.database, call)
    assert blocked.data == {"reason": "dispatch_blocked", "message": "The call was blocked at dispatch."}
    assert (await load_call(r.database, call)).error_message is None


async def _answered(r: Rig, status: CallStatus, answered_ago_s: float, **values: Any) -> SeededCall:
    answered_at = r.clock.now() - timedelta(seconds=answered_ago_s)
    return await _held_call(r, status, answered_at=answered_at, **values)


def _close(value: Decimal | None, expected: float) -> bool:
    return value is not None and abs(float(value) - expected) < 1.0


@pytest.mark.parametrize("reason", ["error", "worker_crash", "fax_detected", "max_duration"])
async def test_an_answered_call_completes_with_its_connected_seconds(
    r: Rig, reason: WorkflowEndReason
) -> None:
    call = await _answered(r, "in_progress", 60)

    result = await _end(r.call_end(), call, reason)

    assert result == EndCallResult(changed=True, status="completed")
    row = await load_call(r.database, call)
    assert (row.status, row.end_reason) == ("completed", reason)
    assert _close(row.connected_seconds, 60)
    assert row.transfer_seconds == 0
    assert await _hold_status(r, call) == "held"
    assert not await _slot_taken(r, call)


async def test_a_transferred_call_splits_connected_and_transfer_seconds(r: Rig) -> None:
    call = await _answered(r, "transferred", 90)
    started = r.clock.now() - timedelta(seconds=30)
    await r.redis.hset(carrier_state_key(call.call_id), "transfer_started_at", started.isoformat())

    await _end(r.call_end(), call, "worker_crash")

    row = await load_call(r.database, call)
    assert row.status == "completed"
    assert _close(row.connected_seconds, 60)
    assert _close(row.transfer_seconds, 30)


async def test_an_end_reason_the_worker_already_stored_wins(r: Rig) -> None:
    call = await _answered(r, "in_progress", 10, end_reason="agent_end_call", error_message="kept")

    await _end(r.call_end(), call, "worker_crash", error_message="ignored")

    row = await load_call(r.database, call)
    assert (row.end_reason, row.ended_by, row.error_message) == ("agent_end_call", "agent", "kept")
    [ended] = await call_event_rows(r.database, call)
    assert ended.data == {"end_reason": "agent_end_call", "ended_by": "agent"}


async def test_an_answered_call_without_an_answer_time_gets_no_durations(r: Rig) -> None:
    call = await r.seed("in_progress")

    await _end(r.call_end(), call, "error")

    row = await load_call(r.database, call)
    assert (row.status, row.connected_seconds) == ("completed", Decimal(0))


@pytest.mark.parametrize("reason", ["canceled", "dispatch_blocked", "carrier_error"])
async def test_a_pre_answer_reason_cannot_end_an_answered_call(r: Rig, reason: WorkflowEndReason) -> None:
    call = await _answered(r, "in_progress", 5)

    with pytest.raises(ApplicationError) as caught:
        await _end(r.call_end(), call, reason)

    assert (caught.value.type, caught.value.non_retryable) == (INVALID_END_REASON, True)
    assert (await load_call(r.database, call)).status == "in_progress"
    assert await _slot_taken(r, call)


@pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES))
async def test_a_terminal_call_is_left_alone_but_frees_its_slot(r: Rig, status: CallStatus) -> None:
    call = await _held_call(r, status)

    assert await _end(r.call_end(), call, "error") == EndCallResult(changed=False, status=status)
    assert await call_event_rows(r.database, call) == []
    assert (await load_call(r.database, call)).status == status
    assert not await _slot_taken(r, call)


@pytest.mark.parametrize(
    ("raced_to", "slot_kept"), [("no_answer", False), ("in_progress", True)], ids=["terminal", "live"]
)
async def test_a_concurrent_status_change_wins(
    r: Rig, monkeypatch: pytest.MonkeyPatch, raced_to: CallStatus, slot_kept: bool
) -> None:
    call = await _held_call(r, "ringing")

    async def raced(self: CallRepo, call_id: uuid.UUID, to: CallStatus, **_values: object) -> None:
        raise CallTransitionError(call_id, to, raced_to)

    monkeypatch.setattr(CallRepo, "transition", raced)

    assert await _end(r.call_end(), call, "canceled") == EndCallResult(changed=False, status=raced_to)
    assert await call_event_rows(r.database, call) == []
    assert await _hold_status(r, call) == "held"
    assert await _slot_taken(r, call) is slot_kept


async def test_an_unreadable_carrier_state_is_retried(r: Rig) -> None:
    call = await r.seed("in_progress")

    with pytest.raises(ApplicationError) as caught:
        await _end(r.call_end(redis=broken_redis()), call, "error")

    assert (caught.value.type, caught.value.non_retryable) == (STATE_UNAVAILABLE, False)


async def test_an_unknown_call_fails_without_retries(r: Rig) -> None:
    with pytest.raises(ApplicationError) as caught:
        await _end(r.call_end(), SeededCall(org_id=new_id(), call_id=new_id()), "error")

    assert (caught.value.type, caught.value.non_retryable) == (CALL_NOT_FOUND, True)
