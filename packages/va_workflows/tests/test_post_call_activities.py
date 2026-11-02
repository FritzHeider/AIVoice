"""The ``PostCallWorkflow`` activities against Postgres (specs/18 §1 steps 1, 2, 11, 13; Decision
P05-T18b)."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from call_rows import SeededCall, load_call, seed_leg
from recording_rig import BUCKET, PRESIGN_HOST, MemoryS3
from sqlalchemy import update
from telephony_rig import Rig, call_event_rows, rig
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from va_common.metrics import Service, build_metrics, new_registry
from va_common.storage import RecordingStorage
from va_db.models import Call
from va_db.repositories import NewTelnyxEvent, TelnyxEventRepo
from va_db.session import Database
from va_workflows.activities.errors import CALL_NOT_ENDED, CALL_NOT_FOUND
from va_workflows.activities.inputs import CallActivityInput
from va_workflows.activities.post_call import RECORDING_MISSING_CODE, PostCallActivities
from va_workflows.activities.post_call_inputs import PostCallStepInput

ANSWERED_AT = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=5)
ENDED_AT = ANSWERED_AT + timedelta(seconds=90)


@pytest.fixture
async def r(database: Database) -> AsyncIterator[Rig]:
    async with rig(database) as built:
        yield built


def _activities(r: Rig, *, s3: MemoryS3 | None = None, registry: Any = None) -> PostCallActivities:
    storage = None if s3 is None else RecordingStorage(s3.store(), BUCKET, None)
    step_ms = (
        None
        if registry is None
        else build_metrics(Service.WORKER, registry).histogram("va_postcall_duration_ms")
    )
    return PostCallActivities(
        database=r.database, publisher=r.publisher, clock=r.clock, storage=storage, step_ms=step_ms
    )


def _input(call: SeededCall) -> CallActivityInput:
    return CallActivityInput(call_id=call.call_id, org_id=call.org_id)


async def _run(fn: Any, arg: Any) -> Any:
    return await ActivityEnvironment().run(fn, arg)


async def _failure(fn: Any, arg: Any) -> ApplicationError:
    with pytest.raises(ApplicationError) as caught:
        await _run(fn, arg)
    return caught.value


async def _connected(r: Rig, **values: Any) -> SeededCall:
    defaults: dict[str, Any] = {
        "answered_at": ANSWERED_AT,
        "ended_at": ENDED_AT,
        "end_reason": "user_hangup",
        "ended_by": "user",
        "connected_seconds": Decimal("90.000"),
    }
    return await r.seed("completed", **{**defaults, **values})


async def _store_webhook(
    database: Database, *, call_id: uuid.UUID | None, ccid: str | None, occurred_at: datetime, rec: str
) -> None:
    payload = {"data": {"event_type": "call.recording.saved", "payload": {"recording_id": rec}}}
    async with database.system_session() as session:
        await TelnyxEventRepo(session).record(
            NewTelnyxEvent(
                telnyx_event_id=f"evt-{secrets.token_hex(8)}",
                event_type="call.recording.saved",
                occurred_at=occurred_at,
                payload=payload,
                call_id=call_id,
                call_control_id=ccid,
            )
        )


# finalize_call_record


async def test_the_record_step_backfills_unset_end_values_from_the_legs(r: Rig) -> None:
    call = await r.seed("completed", answered_at=ANSWERED_AT, end_reason="user_hangup")
    await seed_leg(r.database, call, f"v3:{secrets.token_hex(4)}", ended_at=ENDED_AT, state="hangup")
    await seed_leg(
        r.database,
        call,
        f"v3:{secrets.token_hex(4)}",
        role="transfer_target",
        started_at=ANSWERED_AT + timedelta(seconds=60),
    )
    facts = await _run(_activities(r).finalize_call_record, _input(call))

    row = await load_call(r.database, call)
    assert (row.ended_at, row.ended_by) == (ENDED_AT, "user")
    assert (row.connected_seconds, row.transfer_seconds) == (Decimal("60.000"), Decimal("30.000"))
    # The resolved task config records (``record: true``); this activity instance has no storage.
    assert (facts.connected, facts.recording_wanted, facts.recording_storage, facts.done_steps) == (
        True,
        True,
        False,
        (),
    )


async def test_the_record_step_never_overwrites_stored_values(r: Rig) -> None:
    call = await _connected(r, ended_by="agent", connected_seconds=Decimal("12.500"))
    before = await load_call(r.database, call)
    await _run(_activities(r).finalize_call_record, _input(call))

    after = await load_call(r.database, call)
    assert (after.ended_at, after.ended_by, after.connected_seconds, after.transfer_seconds) == (
        before.ended_at,
        "agent",
        Decimal("12.500"),
        Decimal("0.000"),
    )


async def test_an_unanswered_call_without_an_end_time_gets_the_clock(r: Rig) -> None:
    call = await r.seed("no_answer", end_reason="no_answer")
    facts = await _run(_activities(r).finalize_call_record, _input(call))

    row = await load_call(r.database, call)
    assert row.ended_at is not None
    assert row.ended_by == "system"
    assert row.connected_seconds == 0
    assert facts.connected is False


@pytest.mark.parametrize(
    ("config", "wanted"),
    [
        ({"record": True}, True),
        ({"post_call_evals": {"eval_id": "e"}}, True),
        ({"retranscribe": True}, True),
        ({"record": False, "post_call_evals": None}, False),
    ],
)
async def test_the_recording_is_wanted_for_record_evals_or_retranscription(
    r: Rig, config: dict[str, Any], wanted: bool
) -> None:
    call = await _connected(r, config={**config, "postcall_steps": ["finalize_billing"]})
    facts = await _run(_activities(r, s3=MemoryS3()).finalize_call_record, _input(call))

    assert (facts.recording_wanted, facts.recording_storage, facts.done_steps) == (
        wanted,
        True,
        ("finalize_billing",),
    )


async def test_the_record_step_waits_for_the_end_of_a_live_call(r: Rig) -> None:
    call = await r.seed("in_progress", answered_at=ANSWERED_AT)
    error = await _failure(_activities(r).finalize_call_record, _input(call))

    assert (error.type, error.non_retryable) == (CALL_NOT_ENDED, False)


async def test_an_unknown_call_is_not_found(r: Rig) -> None:
    error = await _failure(
        _activities(r).finalize_call_record, CallActivityInput(call_id=uuid.uuid4(), org_id=uuid.uuid4())
    )

    assert (error.type, error.non_retryable) == (CALL_NOT_FOUND, True)


# find_saved_recording / report_recording_missing


async def test_the_newest_stored_recording_is_found_by_call_or_leg(r: Rig) -> None:
    call = await _connected(r)
    ccid = f"v3:{secrets.token_hex(6)}"
    await seed_leg(r.database, call, ccid)
    activities = _activities(r)
    assert (await _run(activities.find_saved_recording, _input(call))).event is None

    await _store_webhook(r.database, call_id=call.call_id, ccid=None, occurred_at=ENDED_AT, rec="old")
    await _store_webhook(
        r.database, call_id=None, ccid=ccid, occurred_at=ENDED_AT + timedelta(seconds=5), rec="new"
    )
    other = await _connected(r)
    await _store_webhook(
        r.database, call_id=other.call_id, ccid=None, occurred_at=ENDED_AT + timedelta(minutes=1), rec="other"
    )
    found = (await _run(activities.find_saved_recording, _input(call))).event

    assert found is not None
    assert (found.type, found.leg, found.call_id, found.org_id) == (
        "call.recording.saved",
        "primary",
        call.call_id,
        call.org_id,
    )
    assert found.payload == {"recording_id": "new"}
    assert found.occurred_at == ENDED_AT + timedelta(seconds=5)


async def test_a_missing_recording_is_reported_once(r: Rig) -> None:
    call = await _connected(r)
    activities = _activities(r)

    assert await _run(activities.report_recording_missing, _input(call)) is True
    assert await _run(activities.report_recording_missing, _input(call)) is False

    errors = [row for row in await call_event_rows(r.database, call) if row.category == "error"]
    assert [(row.level, row.data, row.message) for row in errors] == [
        ("warning", {"code": RECORDING_MISSING_CODE}, "The call recording did not arrive.")
    ]


# mark_postcall_step


async def test_steps_are_marked_once_in_completion_order(r: Rig) -> None:
    call = await _connected(r, config={"record": True})
    activities = _activities(r)

    def step(name: str) -> PostCallStepInput:
        return PostCallStepInput(call_id=call.call_id, org_id=call.org_id, step=name)  # type: ignore[arg-type]

    assert await _run(activities.mark_postcall_step, step("finalize_call_record")) is True
    assert await _run(activities.mark_postcall_step, step("finalize_billing")) is True
    assert await _run(activities.mark_postcall_step, step("finalize_call_record")) is False

    row = await load_call(r.database, call)
    assert row.config["postcall_steps"] == ["finalize_call_record", "finalize_billing"]
    assert row.config["record"] is True


# finalize_billing / emit_completed


async def test_billing_then_completion_emit_the_call_object_once(r: Rig) -> None:
    registry = new_registry(process_collectors=False)
    s3 = MemoryS3()
    activities = _activities(r, s3=s3, registry=registry)
    call = await _connected(r)
    async with r.database.tenant_session(call.org_id) as session:
        await session.execute(
            update(Call).where(Call.id == call.call_id).values(recording_s3_key=f"{call.org_id}/rec.wav")
        )

    billed = await _run(activities.finalize_billing, _input(call))
    first = await _run(activities.emit_completed, _input(call))
    second = await _run(activities.emit_completed, _input(call))

    assert billed.price_micros > 0
    assert (billed.hold, billed.posted > 0) == ("none", True)
    assert (first.status, second.status) == ("emitted", "already_completed")
    row = await load_call(r.database, call)
    assert (row.queue_status, row.price_micros) == ("complete", billed.price_micros)
    completed = [row for row in await call_event_rows(r.database, call) if row.message == "Call completed."]
    assert len(completed) == 1
    data = completed[0].data
    assert data["call_id"] == str(call.call_id)
    assert data["completed"] is True
    assert data["queue_status"] == "complete"
    assert data["price"] == billed.price_micros / 1_000_000
    assert data["recording_url"].startswith(f"{PRESIGN_HOST}/{BUCKET}/{call.org_id}/rec.wav?")
    assert "expires=604800" in data["recording_url"]
    for step in ("finalize_billing", "emit_completed"):
        assert registry.get_sample_value("va_postcall_duration_ms_count", {"step": step}) == 1 + (
            step == "emit_completed"
        )


async def test_billing_an_unanswered_call_is_free(r: Rig) -> None:
    call = await r.seed("no_answer", end_reason="no_answer", ended_at=ENDED_AT)
    billed = await _run(_activities(r).finalize_billing, _input(call))

    assert billed.price_micros == 0
    assert (await load_call(r.database, call)).price_micros == 0


async def test_billing_waits_for_the_end_and_refuses_unknown_calls(r: Rig) -> None:
    live = await r.seed("in_progress", answered_at=ANSWERED_AT)
    activities = _activities(r)

    not_ended = await _failure(activities.finalize_billing, _input(live))
    missing = await _failure(
        activities.finalize_billing, CallActivityInput(call_id=uuid.uuid4(), org_id=live.org_id)
    )

    assert (not_ended.type, not_ended.non_retryable) == (CALL_NOT_ENDED, False)
    assert (missing.type, missing.non_retryable) == (CALL_NOT_FOUND, True)


async def test_completion_of_a_live_call_writes_nothing(r: Rig) -> None:
    call = await r.seed("in_progress", answered_at=ANSWERED_AT)
    result = await _run(_activities(r).emit_completed, _input(call))

    assert result.status == "not_ended"
    assert (await load_call(r.database, call)).queue_status != "complete"


async def test_completion_without_storage_or_with_a_failing_presign_has_no_url(r: Rig) -> None:
    s3 = MemoryS3(fail_presign=True)
    for activities in (_activities(r), _activities(r, s3=s3)):
        call = await _connected(r, recording_s3_key="org/rec.wav")
        assert (await _run(activities.emit_completed, _input(call))).status == "emitted"
        [event] = [row for row in await call_event_rows(r.database, call) if row.message == "Call completed."]
        assert event.data["recording_url"] is None


async def test_completion_of_an_unknown_call_is_not_found(r: Rig) -> None:
    error = await _failure(
        _activities(r).emit_completed, CallActivityInput(call_id=uuid.uuid4(), org_id=uuid.uuid4())
    )
    assert error.type == CALL_NOT_FOUND
