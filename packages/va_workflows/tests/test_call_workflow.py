"""``CallWorkflow`` on the Temporal time-skipping server with scripted activities (specs/01 §2.1 Decision
P05-T06c)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import pytest
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.testing import WorkflowEnvironment
from workflow_rig import (
    FakeCallActivities,
    Signal,
    boom,
    call_start,
    carrier,
    dialing,
    ends,
    hangups,
    not_found,
    postcall_running,
    run_call,
    workflow_env,
)

from va_domain.call_workflow import (
    CANCEL_SIGNAL,
    PIPELINE_FINISHED_SIGNAL,
    CallWorkflowInput,
    PipelineFinished,
)
from va_domain.post_call import POSTCALL_WORKFLOW_NAME, PostCallWorkflowInput, postcall_workflow_id
from va_workflows.activities.context_inputs import PrefetchResult
from va_workflows.activities.liveness_inputs import CallPresence, WatchInput
from va_workflows.activities.telephony_inputs import (
    DialInput,
    DialOutcome,
    EndCallResult,
)
from va_workflows.calls import CallWorkflow, CallWorkflowResult
from va_workflows.calls.call_workflow import (
    DIAL_FAILED_MESSAGE,
    NOT_ANSWERED_MESSAGE,
    PIPELINE_UNRECORDED_MESSAGE,
    RING_BACKSTOP_MESSAGE,
    STREAM_FAILED_MESSAGE,
    _cause_type,
)
from va_workflows.registration import CALL_WORKFLOWS

PREFETCH = "prefetch_call_context"
WATCH = "watch_call_heartbeat"
CANCEL: Signal = (CANCEL_SIGNAL, None)
FINISHED: Signal = (PIPELINE_FINISHED_SIGNAL, PipelineFinished(end_reason="caller_hangup", ended_by="user"))
UNCHANGED = EndCallResult(changed=False, status="completed")


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with workflow_env() as built:
        yield built


def answered(start: CallWorkflowInput) -> Signal:
    return carrier("call.answered", start)


def trail(fakes: FakeCallActivities) -> tuple[list[str], list[tuple[str, str | None]], list[str]]:
    """Activity names, ``end_call`` reasons/messages, and hangup reasons, in order."""
    return fakes.names(), ends(fakes), hangups(fakes)


def scheduled_at(fakes: FakeCallActivities, name: str) -> list[datetime]:
    return [at for called, at in fakes.scheduled if called == name]


def outcome(result: str, status: str, code: str | None = None) -> DialOutcome:
    return DialOutcome.model_validate({"result": result, "status": status, "code": code})


async def test_a_call_runs_until_the_media_worker_finishes(env: WorkflowEnvironment) -> None:
    start = call_start()
    fakes = FakeCallActivities(
        send={"dial": [answered(start)], WATCH: [FINISHED]},
        end_results=[UNCHANGED],
    )

    run = await run_call(env, fakes, start)

    assert fakes.names() == [PREFETCH, "dial", WATCH, "end_call"]
    assert ends(fakes) == [("error", PIPELINE_UNRECORDED_MESSAGE)]
    assert fakes.inputs("dial", DialInput) == [DialInput(call_id=start.call_id, org_id=start.org_id)]
    [watch] = fakes.inputs(WATCH, WatchInput)
    assert (watch.call_id, watch.missing_after_s, watch.poll_interval_s) == (start.call_id, 20, 3.0)
    assert "EVENT_TYPE_ACTIVITY_TASK_CANCEL_REQUESTED" in run.history
    assert run.result == CallWorkflowResult(outcome="completed", end_reason=None)
    assert run.post_call == PostCallWorkflowInput(call_id=start.call_id, org_id=start.org_id)
    assert "EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED" in run.history
    assert run.phase == "ended"


async def test_an_unrecorded_pipeline_end_is_recorded_by_the_cleanup(env: WorkflowEnvironment) -> None:
    start = call_start()
    fakes = FakeCallActivities(send={"dial": [answered(start)], WATCH: [FINISHED]})

    run = await run_call(env, fakes, start)

    assert run.result == CallWorkflowResult(outcome="completed", end_reason="error")
    assert hangups(fakes) == []


async def test_a_lost_media_worker_has_the_workflow_record_and_hang_up(env: WorkflowEnvironment) -> None:
    start = call_start()
    lost: Signal = (PIPELINE_FINISHED_SIGNAL, PipelineFinished(end_reason="worker_lost", ended_by="error"))
    fakes = FakeCallActivities(
        send={"dial": [answered(start)], WATCH: [lost]},
        end_results=[EndCallResult(changed=True, status="completed")],
    )

    run = await run_call(env, fakes, start)

    assert trail(fakes) == (
        [PREFETCH, "dial", WATCH, "end_call", "hangup_call"],
        [("worker_lost", None)],
        ["worker_lost"],
    )
    assert run.result == CallWorkflowResult(outcome="worker_lost", end_reason="worker_lost")
    assert run.post_call == PostCallWorkflowInput(call_id=start.call_id, org_id=start.org_id)


async def test_a_scheduled_call_is_dialled_at_its_start_time(env: WorkflowEnvironment) -> None:
    scheduled_for = await env.get_current_time() + timedelta(hours=2)
    start = call_start(scheduled_for=scheduled_for)
    fakes = FakeCallActivities(dials=[outcome("failed", "failed", "D11")])

    run = await run_call(env, fakes, start)

    assert fakes.names() == [PREFETCH, "dial", "end_call"]
    [prefetched_at] = scheduled_at(fakes, PREFETCH)
    assert prefetched_at >= scheduled_for
    assert run.result.outcome == "failed"


async def test_a_cancel_while_scheduled_ends_the_call_without_dialling(env: WorkflowEnvironment) -> None:
    start = call_start(scheduled_for=await env.get_current_time() + timedelta(days=1))
    fakes = FakeCallActivities(end_results=[EndCallResult(changed=True, status="canceled")])

    run = await run_call(env, fakes, start, signals=[CANCEL])

    assert fakes.names() == ["end_call"]
    assert ends(fakes) == [("canceled", None)]
    assert run.result == CallWorkflowResult(outcome="canceled", end_reason="canceled")
    assert run.post_call == PostCallWorkflowInput(call_id=start.call_id, org_id=start.org_id)


async def test_a_scheduled_time_in_the_past_starts_at_once(env: WorkflowEnvironment) -> None:
    start = call_start(scheduled_for=await env.get_current_time() - timedelta(minutes=1))
    fakes = FakeCallActivities(dials=[outcome("skipped", "canceled")])

    run = await run_call(env, fakes, start)

    assert fakes.names() == [PREFETCH, "dial", "end_call"]
    assert ends(fakes) == [("canceled", None)]
    assert run.result.outcome == "skipped"


async def test_a_cancel_during_the_prefetch_ends_the_call_before_the_dial(env: WorkflowEnvironment) -> None:
    start = call_start()
    fakes = FakeCallActivities(send={PREFETCH: [CANCEL]})

    run = await run_call(env, fakes, start)

    assert fakes.names() == [PREFETCH, "end_call"]
    assert ends(fakes) == [("canceled", None)]
    assert run.result.outcome == "canceled"


async def test_a_call_already_terminal_at_prefetch_is_only_cleaned_up(env: WorkflowEnvironment) -> None:
    start = call_start()
    fakes = FakeCallActivities(
        prefetch=PrefetchResult(stored=False, status="canceled"),
        end_results=[EndCallResult(changed=False, status="canceled")],
    )

    run = await run_call(env, fakes, start)

    assert fakes.names() == [PREFETCH, "end_call"]
    assert run.result == CallWorkflowResult(outcome="skipped", end_reason=None)
    assert run.post_call is not None


async def test_a_failed_prefetch_does_not_stop_the_call(env: WorkflowEnvironment) -> None:
    start = call_start()
    fakes = FakeCallActivities(prefetch=boom("redis down"), dials=[outcome("failed", "failed")])

    run = await run_call(env, fakes, start)

    assert fakes.names() == [PREFETCH, "dial", "end_call"]
    assert ends(fakes) == [("carrier_error", None)]
    assert run.result.outcome == "failed"


@pytest.mark.parametrize("where", [PREFETCH, "dial"])
async def test_an_unknown_call_ends_the_workflow_without_cleanup(
    env: WorkflowEnvironment, where: str
) -> None:
    start = call_start()
    fakes = FakeCallActivities()
    if where == PREFETCH:
        fakes.prefetch = not_found()
    else:
        fakes.dials = [not_found()]

    run = await run_call(env, fakes, start)

    assert fakes.names() == [PREFETCH, "dial"][: 1 if where == PREFETCH else 2]
    assert run.result == CallWorkflowResult(outcome="not_found")
    assert await postcall_running(env, start) is None


async def test_a_full_concurrency_cap_requeues_the_dial_every_15_seconds(env: WorkflowEnvironment) -> None:
    start = call_start()
    requeue = outcome("requeue", "scheduled", "CONCURRENCY_CAP_REACHED")
    fakes = FakeCallActivities(dials=[requeue, requeue, outcome("blocked", "failed", "DNC_BLOCKED")])

    run = await run_call(env, fakes, start)

    assert [dial.final_attempt for dial in fakes.inputs("dial", DialInput)] == [False, False, False]
    first, second, third = scheduled_at(fakes, "dial")
    assert second - first >= timedelta(seconds=15)
    assert third - second >= timedelta(seconds=15)
    assert ends(fakes) == [("dispatch_blocked", None)]
    assert run.result.outcome == "blocked"


async def test_the_last_requeue_after_30_minutes_is_a_final_attempt(env: WorkflowEnvironment) -> None:
    start = call_start()
    fakes = FakeCallActivities(
        dials=[outcome("requeue", "scheduled", "CONCURRENCY_CAP_REACHED")],
        on_final_attempt=outcome("blocked", "failed", "CONCURRENCY_CAP_REACHED"),
    )

    run = await run_call(env, fakes, start)

    finals = [dial.final_attempt for dial in fakes.inputs("dial", DialInput)]
    assert finals == [False] * 119 + [True]
    times = scheduled_at(fakes, "dial")
    assert times[-1] - times[1] >= timedelta(minutes=30) - timedelta(seconds=30)
    assert run.result.outcome == "blocked"


async def test_a_cancel_while_requeued_ends_the_call(env: WorkflowEnvironment) -> None:
    start = call_start()
    requeue = outcome("requeue", "scheduled", "CONCURRENCY_CAP_REACHED")
    fakes = FakeCallActivities(dials=[requeue], send={"dial": [CANCEL]})

    run = await run_call(env, fakes, start)

    assert fakes.names() == [PREFETCH, "dial", "end_call"]
    assert ends(fakes) == [("canceled", None)]
    assert run.result.outcome == "canceled"


@pytest.mark.parametrize(
    ("result", "status", "reason"),
    [
        ("skipped", "canceled", "canceled"),
        ("blocked", "failed", "dispatch_blocked"),
        ("failed", "failed", "carrier_error"),
    ],
)
async def test_a_dial_that_brings_no_call_up_is_cleaned_up_without_a_hangup(
    env: WorkflowEnvironment, result: str, status: str, reason: str
) -> None:
    start = call_start()
    fakes = FakeCallActivities(
        dials=[outcome(result, status)], end_results=[EndCallResult(changed=False, status=status)]
    )

    run = await run_call(env, fakes, start)

    assert fakes.names() == [PREFETCH, "dial", "end_call"]
    assert ends(fakes) == [(reason, None)]
    assert run.result == CallWorkflowResult.model_validate({"outcome": result})
    assert run.post_call is not None


async def test_a_dial_activity_failure_ends_and_hangs_up(env: WorkflowEnvironment) -> None:
    start = call_start()
    fakes = FakeCallActivities(
        dials=[boom("telnyx unavailable")], end_results=[EndCallResult(changed=True, status="failed")]
    )

    run = await run_call(env, fakes, start)

    assert trail(fakes) == (
        [PREFETCH, "dial", "end_call", "hangup_call"],
        [("error", DIAL_FAILED_MESSAGE)],
        ["dial_failed"],
    )
    assert run.result == CallWorkflowResult(outcome="error", end_reason="error")


async def test_a_hangup_before_the_answer_is_cleaned_up(env: WorkflowEnvironment) -> None:
    start = call_start()
    fakes = FakeCallActivities(
        send={"dial": [carrier("call.hangup", start, payload={"hangup_cause": "no_answer"})]},
        end_results=[EndCallResult(changed=False, status="no_answer")],
    )

    run = await run_call(env, fakes, start)

    assert fakes.names() == [PREFETCH, "dial", "end_call"]
    assert ends(fakes) == [("carrier_error", NOT_ANSWERED_MESSAGE)]
    assert run.result == CallWorkflowResult(outcome="not_answered", end_reason=None)


async def test_a_cancel_while_ringing_ends_then_hangs_up(env: WorkflowEnvironment) -> None:
    start = call_start()
    fakes = FakeCallActivities(
        send={"dial": [CANCEL]}, end_results=[EndCallResult(changed=True, status="canceled")]
    )

    run = await run_call(env, fakes, start)

    assert fakes.names() == [PREFETCH, "dial", "end_call", "hangup_call"]
    assert ends(fakes) == [("canceled", None)]
    assert hangups(fakes) == ["canceled"]
    assert run.result == CallWorkflowResult(outcome="canceled", end_reason="canceled")


async def test_a_cancel_after_the_answer_is_ignored(env: WorkflowEnvironment) -> None:
    start = call_start()
    fakes = FakeCallActivities(
        send={"dial": [answered(start), CANCEL], WATCH: [FINISHED]}, end_results=[UNCHANGED]
    )

    run = await run_call(env, fakes, start)

    assert fakes.names() == [PREFETCH, "dial", WATCH, "end_call"]
    assert ends(fakes) == [("error", PIPELINE_UNRECORDED_MESSAGE)]
    assert run.result.outcome == "completed"


async def test_no_answer_and_no_hangup_hits_the_ring_backstop(env: WorkflowEnvironment) -> None:
    start = call_start()
    fakes = FakeCallActivities(end_results=[EndCallResult(changed=True, status="failed")])

    run = await run_call(env, fakes, start)

    assert trail(fakes) == (
        [PREFETCH, "dial", "end_call", "hangup_call"],
        [("error", RING_BACKSTOP_MESSAGE)],
        ["ring_backstop"],
    )
    [dialled_at] = scheduled_at(fakes, "dial")
    [ended_at] = scheduled_at(fakes, "end_call")
    assert ended_at - dialled_at >= timedelta(seconds=300)
    assert run.result == CallWorkflowResult(outcome="error", end_reason="error")


async def test_a_lost_heartbeat_ends_the_call_as_a_worker_crash(env: WorkflowEnvironment) -> None:
    start = call_start()
    fakes = FakeCallActivities(send={"dial": [answered(start)]}, watch="lose")

    run = await run_call(env, fakes, start)

    assert fakes.names() == [PREFETCH, "dial", WATCH, "end_call", "hangup_call"]
    assert ends(fakes) == [("worker_crash", None)]
    assert hangups(fakes) == ["worker_crash"]
    assert run.result == CallWorkflowResult(outcome="worker_crash", end_reason="worker_crash")


async def test_a_premium_fax_result_ends_and_hangs_up(env: WorkflowEnvironment) -> None:
    start = call_start()
    fax = carrier("call.machine.premium.detection.ended", start, payload={"result": "fax_detected"})
    fakes = FakeCallActivities(send={"dial": [answered(start)], WATCH: [fax]})

    run = await run_call(env, fakes, start)

    assert fakes.names() == [PREFETCH, "dial", WATCH, "end_call", "hangup_call"]
    assert ends(fakes) == [("fax_detected", None)]
    assert hangups(fakes) == ["fax_detected"]
    assert run.result == CallWorkflowResult(outcome="fax_detected", end_reason="fax_detected")


async def test_fax_events_of_other_legs_and_redeliveries_are_ignored(env: WorkflowEnvironment) -> None:
    start = call_start()
    payload = {"result": "fax_detected"}
    other_leg = carrier(
        "call.machine.premium.detection.ended", start, leg="transfer_target", payload=payload, event_id="e-1"
    )
    redelivered = carrier("call.machine.premium.detection.ended", start, payload=payload, event_id="e-1")
    human = carrier("call.machine.premium.detection.ended", start, payload={"result": "human_residence"})
    fakes = FakeCallActivities(
        send={"dial": [answered(start)], WATCH: [other_leg, redelivered, human, FINISHED]},
        end_results=[UNCHANGED],
    )

    run = await run_call(env, fakes, start)

    assert fakes.names() == [PREFETCH, "dial", WATCH, "end_call"]
    assert run.result.outcome == "completed"


async def test_a_failed_stream_without_an_owner_ends_the_call(env: WorkflowEnvironment) -> None:
    start = call_start()
    fakes = FakeCallActivities(
        send={"dial": [answered(start), carrier("streaming.failed", start)]},
        watch="fail",
        presence=[CallPresence(owner=False, heartbeat=False)],
    )

    run = await run_call(env, fakes, start)

    assert fakes.names() == [PREFETCH, "dial", WATCH, "call_presence", "end_call", "hangup_call"]
    assert ends(fakes) == [("error", STREAM_FAILED_MESSAGE)]
    assert hangups(fakes) == ["stream_failed"]
    [dialled_at] = scheduled_at(fakes, "dial")
    [checked_at] = scheduled_at(fakes, "call_presence")
    assert checked_at - dialled_at >= timedelta(seconds=5)
    assert run.result == CallWorkflowResult(outcome="stream_failed", end_reason="error")


@pytest.mark.parametrize("presence", [CallPresence(owner=True, heartbeat=True), boom("redis down")])
async def test_a_failed_stream_with_an_owner_or_an_unknown_presence_continues(
    env: WorkflowEnvironment, presence: CallPresence | ApplicationError
) -> None:
    start = call_start()
    fakes = FakeCallActivities(
        send={"dial": [answered(start), carrier("streaming.failed", start)], "call_presence": [FINISHED]},
        watch="fail",
        presence=[presence],
        end_results=[UNCHANGED],
    )

    run = await run_call(env, fakes, start)

    assert fakes.names() == [PREFETCH, "dial", WATCH, "call_presence", "end_call"]
    assert run.result.outcome == "completed"


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        (True, CallWorkflowResult(outcome="worker_crash", end_reason="worker_crash")),
        (False, CallWorkflowResult(outcome="completed")),
    ],
)
async def test_a_hangup_without_a_pipeline_end_is_cleaned_up_after_30_seconds(
    env: WorkflowEnvironment, changed: bool, expected: CallWorkflowResult
) -> None:
    start = call_start()
    fakes = FakeCallActivities(
        send={"dial": [answered(start), carrier("call.hangup", start)]},
        watch="fail",
        end_results=[EndCallResult(changed=changed, status="completed")],
    )

    run = await run_call(env, fakes, start)

    assert fakes.names() == [PREFETCH, "dial", "end_call"]
    assert ends(fakes) == [("worker_crash", None)]
    [dialled_at] = scheduled_at(fakes, "dial")
    [ended_at] = scheduled_at(fakes, "end_call")
    assert ended_at - dialled_at >= timedelta(seconds=30)
    assert run.result == expected


async def test_a_pipeline_end_within_the_hangup_grace_completes_the_call(env: WorkflowEnvironment) -> None:
    start = call_start()
    fakes = FakeCallActivities(
        send={"dial": [answered(start)], WATCH: [carrier("call.hangup", start)]},
        on_watch_cancel=[FINISHED],
        end_results=[UNCHANGED],
    )

    run = await run_call(env, fakes, start)

    assert fakes.names() == [PREFETCH, "dial", WATCH, "end_call"]
    assert ends(fakes) == [("error", PIPELINE_UNRECORDED_MESSAGE)]
    assert run.result == CallWorkflowResult(outcome="completed")


@pytest.mark.parametrize(("max_duration_s", "backstop_s"), [(60, 180), (None, 14_520)])
async def test_the_max_duration_backstop_ends_and_hangs_up(
    env: WorkflowEnvironment, max_duration_s: int | None, backstop_s: int
) -> None:
    start = call_start()
    fakes = FakeCallActivities(
        dials=[dialing(max_duration_s)], send={"dial": [answered(start)]}, watch="fail"
    )

    run = await run_call(env, fakes, start)

    assert fakes.names() == [PREFETCH, "dial", WATCH, "end_call", "hangup_call"]
    assert ends(fakes) == [("max_duration", None)]
    assert hangups(fakes) == ["max_duration"]
    [dialled_at] = scheduled_at(fakes, "dial")
    [ended_at] = scheduled_at(fakes, "end_call")
    assert timedelta(seconds=backstop_s) <= ended_at - dialled_at < timedelta(seconds=backstop_s + 10)
    assert run.result == CallWorkflowResult(outcome="max_duration", end_reason="max_duration")


async def test_a_failing_end_call_still_hangs_up_and_fails_the_workflow(env: WorkflowEnvironment) -> None:
    start = call_start()
    fakes = FakeCallActivities(send={"dial": [answered(start)]}, watch="lose", end_results=[boom("db down")])

    with pytest.raises(WorkflowFailureError):
        await run_call(env, fakes, start)

    assert fakes.names() == [PREFETCH, "dial", WATCH, "end_call", "hangup_call"]
    assert await postcall_running(env, start) is None


async def test_a_failing_hangup_does_not_stop_the_workflow(env: WorkflowEnvironment) -> None:
    start = call_start()
    fakes = FakeCallActivities(send={"dial": [answered(start)]}, watch="lose", hangup=boom("telnyx refused"))

    run = await run_call(env, fakes, start)

    assert fakes.names() == [PREFETCH, "dial", WATCH, "end_call", "hangup_call"]
    assert run.result.outcome == "worker_crash"
    assert run.post_call is not None


async def test_an_existing_post_call_workflow_is_kept(env: WorkflowEnvironment) -> None:
    start = call_start()
    await env.client.start_workflow(
        POSTCALL_WORKFLOW_NAME,
        PostCallWorkflowInput(call_id=start.call_id, org_id=start.org_id),
        id=postcall_workflow_id(start.call_id),
        task_queue="va-test-nobody",
    )
    fakes = FakeCallActivities(dials=[outcome("failed", "failed")])

    run = await run_call(env, fakes, start, await_post_call=False)

    assert run.result.outcome == "failed"
    assert await postcall_running(env, start) is True


def test_the_cause_type_names_non_application_failures() -> None:
    error = ActivityError(
        "activity failed",
        scheduled_event_id=1,
        started_event_id=2,
        identity="worker",
        activity_type="dial",
        activity_id="1",
        retry_state=None,
    )
    error.__cause__ = TimeoutError("heartbeat")
    assert _cause_type(error) == "TimeoutError"
    error.__cause__ = ApplicationError("boom", type="Boom")
    assert _cause_type(error) == "Boom"
    error.__cause__ = ApplicationError("untyped")
    assert _cause_type(error) == "ApplicationError"


def test_the_call_workflow_is_registered_for_the_main_queue() -> None:
    assert (CallWorkflow,) == CALL_WORKFLOWS
