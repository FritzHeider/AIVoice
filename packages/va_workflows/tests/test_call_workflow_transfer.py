"""``CallWorkflow`` after a bridged cold transfer: the ``transferred`` phase (specs/01 §2.1 Decision
P05-T13)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.testing import WorkflowEnvironment
from workflow_rig import (
    FakeCallActivities,
    Signal,
    call_start,
    carrier,
    dialing,
    ends,
    hangups,
    history_events,
    run_call,
    workflow_env,
)

from va_domain.call_workflow import PIPELINE_FINISHED_SIGNAL, CallWorkflowInput, PipelineFinished
from va_workflows.activities.telephony_inputs import EndCallResult
from va_workflows.calls import CallWorkflowResult

PREFETCH = "prefetch_call_context"
WATCH = "watch_call_heartbeat"
TRANSFERRED: Signal = (
    PIPELINE_FINISHED_SIGNAL,
    PipelineFinished(end_reason="transferred", ended_by="system"),
)


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with workflow_env() as built:
        yield built


def target_hangup(start: CallWorkflowInput) -> Signal:
    return carrier("call.hangup", start, leg="transfer_target")


async def test_the_caller_hanging_up_after_a_transfer_ends_it_without_a_hangup(
    env: WorkflowEnvironment,
) -> None:
    start = call_start()
    fakes = FakeCallActivities(
        send={"dial": [carrier("call.answered", start)], WATCH: [TRANSFERRED, target_hangup(start)]},
        on_watch_cancel=[carrier("call.hangup", start)],
        end_results=[EndCallResult(changed=False, status="completed")],
    )

    run = await run_call(env, fakes, start)

    assert fakes.names() == [PREFETCH, "dial", WATCH, "end_call"]
    assert ends(fakes) == [("transferred", None)]
    assert hangups(fakes) == []
    assert run.result == CallWorkflowResult(outcome="completed", end_reason=None)
    assert run.phase == "ended"


async def test_an_unrecorded_transferred_call_is_recorded_by_the_cleanup(env: WorkflowEnvironment) -> None:
    start = call_start()
    fakes = FakeCallActivities(
        send={"dial": [carrier("call.answered", start)], WATCH: [TRANSFERRED]},
        on_watch_cancel=[carrier("call.hangup", start)],
        end_results=[EndCallResult(changed=True, status="completed")],
    )

    run = await run_call(env, fakes, start)

    assert run.result == CallWorkflowResult(outcome="completed", end_reason="transferred")
    assert hangups(fakes) == []


async def test_a_transferred_call_still_up_at_the_backstop_is_ended(env: WorkflowEnvironment) -> None:
    start = call_start()
    fakes = FakeCallActivities(
        dials=[dialing(60)],
        # Everything arrives with the dial, so the watch is started and cancelled in one workflow task and
        # never dispatched (cancelling a dispatched watch that is failing or completing wedges the
        # time-skipping server); a parked watch would block time skipping.
        send={"dial": [carrier("call.answered", start), TRANSFERRED, target_hangup(start)]},
        end_results=[EndCallResult(changed=True, status="completed")],
    )

    run = await run_call(env, fakes, start)

    assert fakes.names() == [PREFETCH, "dial", "end_call", "hangup_call"]
    assert ends(fakes) == [("max_duration", None)]
    assert hangups(fakes) == ["max_duration"]
    assert run.result == CallWorkflowResult(outcome="max_duration", end_reason="max_duration")
    timers = [
        event.timer_started_event_attributes.start_to_fire_timeout.ToTimedelta()
        for event in await history_events(env, start)
        if EventType.Name(event.event_type) == "EVENT_TYPE_TIMER_STARTED"
    ]
    # The transferred phase waits for the rest of the 60 s + 120 s backstop (never longer).
    assert timers
    assert timedelta(seconds=150) < timers[-1] <= timedelta(seconds=180)
