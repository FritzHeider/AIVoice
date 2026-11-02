"""``schedule_retry_call`` against Postgres (specs/05 §3 ``retry``; specs/07 §14 Decision P05-T10f)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from call_rows import SeededCall, load_call
from compliance_rows import add_dnc
from resolved_configs import resolved_config
from sqlalchemy import select
from telephony_rig import CALLEE, CALLER, Rig, call_event_rows, rig
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from va_db.call_context import config_of
from va_db.models import Call, CallStatus
from va_db.session import Database
from va_domain.call_retry import RETRY_ATTEMPT_KEY
from va_domain.call_workflow import call_workflow_id
from va_events import SCHEDULED_MESSAGE
from va_workflows.activities.errors import CALL_NOT_FOUND
from va_workflows.activities.inputs import CallActivityInput
from va_workflows.activities.retry import COPIED_COLUMNS
from va_workflows.activities.retry_inputs import RetryPlan
from va_workflows.activities.telephony_inputs import DialInput

WAIT_S = 600
RETRY: dict[str, Any] = {
    "wait": WAIT_S,
    "voicemail_action": "leave_message",
    "voicemail_message": "Sorry we missed you, we will call back.",
}


@pytest.fixture
async def r(database: Database) -> AsyncIterator[Rig]:
    async with rig(database) as built:
        yield built


def _config(retry: dict[str, Any] | None = RETRY, **extra: Any) -> dict[str, Any]:
    config: dict[str, Any] = {**resolved_config("01_task_basic.json"), "pricing": {"per_minute": 90_000}}
    config["voicemail"] = {**config["voicemail"], "sensitive": True, "message": "Parent message."}
    if retry is not None:
        config["retry"] = retry
    return {**config, **extra}


async def _ended(
    r: Rig,
    end_reason: str | None = "voicemail_left",
    *,
    status: CallStatus = "completed",
    retry: dict[str, Any] | None = RETRY,
    org_id: uuid.UUID | None = None,
    **extra: Any,
) -> SeededCall:
    return await r.seed(
        status,
        org_id=org_id,
        config=_config(retry, **extra),
        end_reason=end_reason,
        external_id="crm-42",
        metadata_={"source": "test"},
        request_data={"name": "Ada"},
    )


async def _schedule(r: Rig, call: SeededCall, *, org_id: uuid.UUID | None = None) -> RetryPlan:
    request = CallActivityInput(call_id=call.call_id, org_id=org_id or call.org_id)
    return await ActivityEnvironment().run(r.retry().schedule_retry_call, request)


async def _children(r: Rig, parent: SeededCall) -> list[Call]:
    async with r.database.tenant_session(parent.org_id) as session:
        rows = await session.scalars(
            select(Call).where(Call.org_id == parent.org_id, Call.parent_call_id == parent.call_id)
        )
        return list(rows.all())


async def test_a_voicemail_end_creates_a_scheduled_child_call(r: Rig) -> None:
    parent = await _ended(r)
    before = r.clock.now()

    plan = await _schedule(r, parent)

    assert plan.reason is None
    assert plan.attempt == 1
    assert plan.scheduled_for is not None
    assert (
        before + timedelta(seconds=WAIT_S) <= plan.scheduled_for <= r.clock.now() + timedelta(seconds=WAIT_S)
    )
    [child] = await _children(r, parent)
    stored = await load_call(r.database, parent)
    assert child.id == plan.child_call_id
    assert (child.status, child.queue_status, child.start_time) == ("scheduled", "new", plan.scheduled_for)
    assert (child.credit_hold_id, child.workflow_id) == (None, call_workflow_id(child.id))
    assert (child.to_number, child.from_number) == (CALLEE, CALLER)
    assert {name: getattr(child, name) for name in COPIED_COLUMNS} == {
        name: getattr(stored, name) for name in COPIED_COLUMNS
    }
    assert (child.end_reason, child.batch_id) == (None, None)
    voicemail = child.config["voicemail"]
    assert (voicemail["action"], voicemail["message"], voicemail["sensitive"]) == (
        "leave_message",
        RETRY["voicemail_message"],
        True,
    )
    assert child.config[RETRY_ATTEMPT_KEY] == 1
    assert child.config["pricing"] == {"per_minute": 90_000}
    assert child.config["retry"] == RETRY
    assert config_of(child).start_time == plan.scheduled_for
    [event] = await call_event_rows(r.database, SeededCall(org_id=parent.org_id, call_id=child.id))
    assert (event.category, event.level, event.message) == ("queue", "info", SCHEDULED_MESSAGE)
    assert event.data == {
        "status": "scheduled",
        "scheduled_for": child.config["start_time"],
        "warnings": [],
        "parent_call_id": str(parent.call_id),
        "retry_attempt": 1,
    }


async def test_a_retry_without_a_message_keeps_the_parent_message(r: Rig) -> None:
    parent = await _ended(r, "voicemail_hangup", retry={"wait": 60})

    plan = await _schedule(r, parent)

    [child] = await _children(r, parent)
    assert plan.attempt == 1
    assert (child.config["voicemail"]["action"], child.config["voicemail"]["message"]) == (
        "hangup",
        "Parent message.",
    )


@pytest.mark.parametrize(
    ("end_reason", "on_no_answer", "created"),
    [
        ("no_answer", True, True),
        ("busy", True, True),
        ("no_answer", False, False),
        ("busy", False, False),
        ("user_hangup", True, False),
        (None, True, False),
    ],
)
async def test_no_answer_and_busy_retry_only_with_on_no_answer(
    r: Rig, end_reason: str | None, on_no_answer: bool, created: bool
) -> None:
    status: CallStatus = "failed" if end_reason in {"no_answer", "busy"} else "completed"
    parent = await _ended(r, end_reason, status=status, retry={**RETRY, "on_no_answer": on_no_answer})

    plan = await _schedule(r, parent)

    assert (plan.reason is None) is created
    if not created:
        assert plan == RetryPlan(reason="end_reason")
    assert len(await _children(r, parent)) == int(created)


async def test_a_call_without_retry_is_skipped(r: Rig) -> None:
    parent = await _ended(r, retry=None)

    assert await _schedule(r, parent) == RetryPlan(reason="no_retry")
    assert await _children(r, parent) == []


async def test_a_call_that_has_not_ended_is_skipped(r: Rig) -> None:
    parent = await _ended(r, None, status="in_progress")

    assert await _schedule(r, parent) == RetryPlan(reason="not_ended")


async def test_the_last_attempt_of_the_chain_is_not_retried(r: Rig) -> None:
    extra: dict[str, Any] = {RETRY_ATTEMPT_KEY: 2}
    parent = await _ended(r, retry={**RETRY, "max_attempts": 2}, **extra)

    assert await _schedule(r, parent) == RetryPlan(reason="attempts_exhausted")
    assert await _children(r, parent) == []


async def test_a_retry_child_ending_on_voicemail_continues_the_chain(r: Rig) -> None:
    parent = await _ended(r, retry={**RETRY, "max_attempts": 2})
    first = await _schedule(r, parent)
    assert first.child_call_id is not None
    async with r.database.tenant_session(parent.org_id) as session:
        child = await session.get_one(Call, first.child_call_id)
        child.status = "completed"
        child.end_reason = "voicemail_left"
    child_call = SeededCall(org_id=parent.org_id, call_id=first.child_call_id)

    second = await _schedule(r, child_call)

    assert (second.reason, second.attempt) == (None, 2)
    [grandchild] = await _children(r, child_call)
    assert grandchild.config[RETRY_ATTEMPT_KEY] == 2
    async with r.database.tenant_session(parent.org_id) as session:
        exhausted = await session.get_one(Call, grandchild.id)
        exhausted.status = "completed"
        exhausted.end_reason = "voicemail_hangup"
    assert await _schedule(r, SeededCall(org_id=parent.org_id, call_id=grandchild.id)) == RetryPlan(
        reason="attempts_exhausted"
    )


async def test_a_repeated_attempt_returns_the_existing_child(r: Rig) -> None:
    parent = await _ended(r)
    first = await _schedule(r, parent)

    again = await _schedule(r, parent)

    assert again == RetryPlan(
        child_call_id=first.child_call_id,
        scheduled_for=first.scheduled_for,
        attempt=1,
        reason="already_scheduled",
    )
    assert len(await _children(r, parent)) == 1


async def test_another_orgs_call_is_not_found(r: Rig) -> None:
    parent = await _ended(r)
    other = await _ended(r)

    with pytest.raises(ApplicationError) as raised:
        await _schedule(r, parent, org_id=other.org_id)

    assert (raised.value.type, raised.value.non_retryable) == (CALL_NOT_FOUND, True)
    assert await _children(r, parent) == []


async def test_the_child_dial_admission_records_a_refusal_on_the_child(r: Rig) -> None:
    parent = await _ended(r)
    plan = await _schedule(r, parent)
    assert plan.child_call_id is not None
    await add_dnc(r.database, parent.org_id, CALLEE)
    child = SeededCall(org_id=parent.org_id, call_id=plan.child_call_id)

    outcome = await ActivityEnvironment().run(
        r.telephony().dial, DialInput(call_id=child.call_id, org_id=child.org_id)
    )

    assert (outcome.result, outcome.status) == ("blocked", "failed")
    row = await load_call(r.database, child)
    assert (row.status, row.end_reason) == ("failed", "dispatch_blocked")
    assert (await load_call(r.database, parent)).end_reason == "voicemail_left"
    assert r.dials() == []
