"""``CallWorkflow`` in inbound mode on the Temporal time-skipping server (specs/01 §2.1 Decision P05-T12c).

The edge creates and answers inbound calls, so the workflow skips the prefetch and the dial: it waits up to
30 s for the answer and then guards the conversation with the input's ``max_duration_s``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from temporalio.testing import WorkflowEnvironment
from workflow_rig import (
    FakeCallActivities,
    Signal,
    carrier,
    ends,
    first_event,
    hangups,
    history_events,
    run_call,
    workflow_env,
)

from va_domain.call_workflow import (
    CANCEL_SIGNAL,
    PIPELINE_FINISHED_SIGNAL,
    CallWorkflowInput,
    PipelineFinished,
)
from va_domain.post_call import PostCallWorkflowInput
from va_workflows.activities.telephony_inputs import EndCallResult
from va_workflows.calls import CallWorkflowResult
from va_workflows.calls.call_workflow import (
    ANSWER_BACKSTOP_MESSAGE,
    NOT_ANSWERED_MESSAGE,
    PIPELINE_UNRECORDED_MESSAGE,
)
from va_workflows.calls.policies import INBOUND_ANSWER_BACKSTOP, MAX_DURATION_GRACE

WATCH = "watch_call_heartbeat"
FINISHED: Signal = (PIPELINE_FINISHED_SIGNAL, PipelineFinished(end_reason="user_hangup", ended_by="user"))
CHANGED = EndCallResult(changed=True, status="failed")


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with workflow_env() as built:
        yield built


def inbound(max_duration_s: int = 600) -> CallWorkflowInput:
    return CallWorkflowInput(
        call_id=uuid.uuid4(), org_id=uuid.uuid4(), direction="inbound", max_duration_s=max_duration_s
    )


async def test_an_answered_inbound_call_converses_without_prefetch_or_dial(env: WorkflowEnvironment) -> None:
    start = inbound()
    fakes = FakeCallActivities(send={WATCH: [FINISHED]})

    run = await run_call(env, fakes, start, signals=[carrier("call.answered", start)])

    assert fakes.names() == [WATCH, "end_call"]
    assert ends(fakes) == [("error", PIPELINE_UNRECORDED_MESSAGE)]
    assert run.result.outcome == "completed"
    assert run.post_call == PostCallWorkflowInput(call_id=start.call_id, org_id=start.org_id)
    assert len(fakes.retry_calls(start.call_id)) == 1
    assert run.phase == "ended"


async def test_a_caller_hanging_up_before_the_answer_is_not_answered(env: WorkflowEnvironment) -> None:
    start = inbound()
    fakes = FakeCallActivities(end_results=[CHANGED])

    run = await run_call(env, fakes, start, signals=[carrier("call.hangup", start)])

    assert fakes.names() == ["end_call"]
    assert ends(fakes) == [("carrier_error", NOT_ANSWERED_MESSAGE)]
    assert hangups(fakes) == []
    assert run.result == CallWorkflowResult(outcome="not_answered", end_reason="carrier_error")


async def test_no_answer_within_the_backstop_ends_and_hangs_up(env: WorkflowEnvironment) -> None:
    start = inbound()
    fakes = FakeCallActivities(end_results=[CHANGED])

    run = await run_call(env, fakes, start)

    assert fakes.names() == ["end_call", "hangup_call"]
    assert ends(fakes) == [("error", ANSWER_BACKSTOP_MESSAGE)]
    assert hangups(fakes) == ["answer_backstop"]
    assert run.result == CallWorkflowResult(outcome="error", end_reason="error")
    assert timedelta(seconds=30) == INBOUND_ANSWER_BACKSTOP


async def test_a_cancel_while_ringing_cancels_and_hangs_up(env: WorkflowEnvironment) -> None:
    start = inbound()
    fakes = FakeCallActivities(end_results=[EndCallResult(changed=True, status="canceled")])

    run = await run_call(env, fakes, start, signals=[(CANCEL_SIGNAL, None)])

    assert ends(fakes) == [("canceled", None)]
    assert hangups(fakes) == ["canceled"]
    assert run.result == CallWorkflowResult(outcome="canceled", end_reason="canceled")


async def test_the_max_duration_backstop_uses_the_input(env: WorkflowEnvironment) -> None:
    start = inbound(max_duration_s=60)
    fakes = FakeCallActivities(watch="fail", end_results=[EndCallResult(changed=True, status="completed")])

    run = await run_call(env, fakes, start, signals=[carrier("call.answered", start)])

    assert fakes.names() == [WATCH, "end_call", "hangup_call"]
    assert hangups(fakes) == ["max_duration"]
    assert run.result == CallWorkflowResult(outcome="max_duration", end_reason="max_duration")
    # The conversation timer starts in the workflow task that schedules the watch, so its duration is exact.
    events = await history_events(env, start)
    watch_scheduled = events.index(first_event(events, "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED"))
    timer = first_event(events[watch_scheduled:], "EVENT_TYPE_TIMER_STARTED")
    fires_in = timer.timer_started_event_attributes.start_to_fire_timeout.ToTimedelta()
    assert fires_in == timedelta(seconds=60) + MAX_DURATION_GRACE
