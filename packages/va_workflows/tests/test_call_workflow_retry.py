"""``CallWorkflow`` voicemail retries on the time-skipping server (specs/07 §14 Decision P05-T10f)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from workflow_rig import (
    RETRY,
    TASK_QUEUE,
    FakeCallActivities,
    Signal,
    boom,
    call_start,
    carrier,
    dialing,
    not_found,
    run_call,
    workflow_env,
)

from va_domain.call_workflow import (
    CALL_WORKFLOW_NAME,
    PIPELINE_FINISHED_SIGNAL,
    CallWorkflowInput,
    PipelineFinished,
    call_workflow_id,
)
from va_workflows.activities.inputs import CallActivityInput
from va_workflows.activities.retry_inputs import RetryPlan
from va_workflows.activities.telephony_inputs import DialOutcome, EndCallResult
from va_workflows.calls import CallWorkflowResult

PREFETCH = "prefetch_call_context"
WATCH = "watch_call_heartbeat"
VOICEMAIL_LEFT: Signal = (
    PIPELINE_FINISHED_SIGNAL,
    PipelineFinished(end_reason="voicemail_left", ended_by="agent"),
)
UNCHANGED = EndCallResult(changed=False, status="completed")
NO_RETRY = RetryPlan(reason="no_retry")


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with workflow_env() as built:
        yield built


def not_answered() -> DialOutcome:
    return DialOutcome.model_validate({"result": "failed", "status": "failed", "code": "D11"})


def child_plan(at: datetime, child: uuid.UUID | None = None) -> RetryPlan:
    return RetryPlan(child_call_id=child or uuid.uuid4(), scheduled_for=at, attempt=1)


def voicemail_call(start: CallWorkflowInput, retry: RetryPlan | Exception) -> FakeCallActivities:
    """An answered call whose media worker ended it on voicemail. A retry child started by the parent
    shares these fakes and may run before the asserts; its own retry request gets ``NO_RETRY``."""
    return FakeCallActivities(
        send={"dial": [carrier("call.answered", start)], WATCH: [VOICEMAIL_LEFT]},
        end_results=[UNCHANGED],
        retry=[retry, NO_RETRY],
    )


async def child_workflow(
    env: WorkflowEnvironment, child: uuid.UUID
) -> tuple[WorkflowHandle[Any, Any], list[Any]]:
    """The retry child's handle and history events (``stop`` it afterwards)."""
    handle = env.client.get_workflow_handle(call_workflow_id(child))
    return handle, list((await handle.fetch_history()).events)


def first_event(events: list[Any], name: str) -> Any:
    return next(event for event in events if EventType.Name(event.event_type) == name)


async def stop(handle: WorkflowHandle[Any, Any]) -> None:
    """Terminate the child unless it already finished (the server may have skipped its timer)."""
    if (await handle.describe()).close_time is None:
        await handle.terminate()


async def test_a_voicemail_end_starts_the_retry_child_workflow(env: WorkflowEnvironment) -> None:
    start = call_start()
    scheduled_for = await env.get_current_time() + timedelta(hours=1)
    plan = child_plan(scheduled_for)
    fakes = voicemail_call(start, plan)

    run = await run_call(env, fakes, start)

    child = plan.child_call_id
    assert child is not None
    assert run.result == CallWorkflowResult(outcome="completed", end_reason=None, retry_call_id=child)
    assert run.phase == "ended"
    assert [name for name, _ in fakes.calls_of(start.call_id)] == [PREFETCH, "dial", WATCH, "end_call", RETRY]
    assert fakes.retry_calls(start.call_id) == [CallActivityInput(call_id=start.call_id, org_id=start.org_id)]
    assert run.history.count("EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED") == 2
    handle, events = await child_workflow(env, child)
    description = await handle.describe()
    assert description.workflow_type == CALL_WORKFLOW_NAME
    assert description.parent_id == start.workflow_id()
    assert description.task_queue == TASK_QUEUE
    started = events[0].workflow_execution_started_event_attributes
    [payload] = started.input.payloads
    assert pydantic_data_converter.payload_converter.from_payload(payload, CallWorkflowInput) == (
        CallWorkflowInput(call_id=child, org_id=start.org_id, scheduled_for=scheduled_for)
    )
    timer = first_event(events, "EVENT_TYPE_TIMER_STARTED")
    fires_in = timer.timer_started_event_attributes.start_to_fire_timeout.ToTimedelta()
    assert timedelta(minutes=59) < fires_in <= timedelta(hours=1)
    await stop(handle)


async def test_an_unanswered_call_asks_for_a_retry_too(env: WorkflowEnvironment) -> None:
    start = call_start()
    plan = child_plan(await env.get_current_time() + timedelta(minutes=5))
    fakes = FakeCallActivities(
        dials=[dialing(), not_answered()],
        send={"dial": [carrier("call.hangup", start, payload={"hangup_cause": "no_answer"})]},
        end_results=[EndCallResult(changed=False, status="failed")],
        retry=[plan, NO_RETRY],
    )

    run = await run_call(env, fakes, start)

    assert run.result == CallWorkflowResult(
        outcome="not_answered", end_reason=None, retry_call_id=plan.child_call_id
    )
    assert fakes.retry_calls(start.call_id) == [CallActivityInput(call_id=start.call_id, org_id=start.org_id)]
    assert plan.child_call_id is not None
    handle, _ = await child_workflow(env, plan.child_call_id)
    assert (await handle.describe()).parent_id == start.workflow_id()
    await stop(handle)


@pytest.mark.parametrize("reason", ["no_retry", "end_reason", "attempts_exhausted", "not_ended"])
async def test_a_skip_plan_starts_no_child(env: WorkflowEnvironment, reason: str) -> None:
    start = call_start()
    fakes = voicemail_call(start, RetryPlan.model_validate({"reason": reason}))

    run = await run_call(env, fakes, start)

    assert run.result.retry_call_id is None
    assert len(fakes.retry_calls(start.call_id)) == 1
    assert "EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED" in run.history
    assert run.history.count("EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED") == 1


async def test_a_failing_retry_activity_still_completes_the_call(env: WorkflowEnvironment) -> None:
    start = call_start()
    fakes = voicemail_call(start, boom("database down"))

    run = await run_call(env, fakes, start)

    assert run.result == CallWorkflowResult(outcome="completed", end_reason=None)
    assert run.phase == "ended"
    assert run.history.count("EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED") == 1


async def test_an_unknown_call_never_asks_for_a_retry(env: WorkflowEnvironment) -> None:
    start = call_start()
    fakes = FakeCallActivities(prefetch=not_found())

    run = await run_call(env, fakes, start)

    assert run.result == CallWorkflowResult(outcome="not_found")
    assert fakes.retry_calls(start.call_id) == []


async def test_an_already_running_retry_child_is_kept(env: WorkflowEnvironment) -> None:
    start = call_start()
    child = uuid.uuid4()
    scheduled_for = await env.get_current_time() + timedelta(days=1)
    existing = await env.client.start_workflow(
        CALL_WORKFLOW_NAME,
        CallWorkflowInput(call_id=child, org_id=start.org_id, scheduled_for=scheduled_for),
        id=call_workflow_id(child),
        task_queue="va-test-nobody",
    )
    plan = RetryPlan(child_call_id=child, scheduled_for=scheduled_for, attempt=1, reason="already_scheduled")
    fakes = voicemail_call(start, plan)

    run = await run_call(env, fakes, start)

    assert run.result.retry_call_id == child
    description = await existing.describe()
    assert description.close_time is None
    assert description.parent_id is None
    await existing.terminate()
