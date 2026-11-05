"""``PostCallWorkflow`` on the time-skipping server with scripted activities (specs/18 §1, Decision
P05-T18b)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from postcall_rig import (
    CONNECTED_RECORDED,
    FakePostCallActivities,
    facts,
    postcall_start,
    recording_event,
    run_post_call,
)
from temporalio.testing import WorkflowEnvironment
from workflow_rig import boom, not_found, workflow_env

from va_domain.post_call import POSTCALL_BUDGET_S, RECORDING_WAIT_S, PostCallTimings
from va_workflows.activities.errors import RECORDING_BUSY, retryable
from va_workflows.activities.post_call_inputs import SavedRecording
from va_workflows.activities.recording import BUSY_MESSAGE
from va_workflows.activities.recording_inputs import StoreRecordingResult
from va_workflows.post_call.budget import STORE_RESERVE


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with workflow_env() as started:
        yield started


async def test_a_connected_recorded_call_runs_every_step_in_order(env: WorkflowEnvironment) -> None:
    start = postcall_start()
    event = recording_event(start)
    fakes = FakePostCallActivities(record=CONNECTED_RECORDED, found=[SavedRecording(event=event)])
    run = await run_post_call(env, fakes, start)

    assert fakes.names() == [
        "finalize_call_record",
        "find_saved_recording",
        "store_recording",
        "finalize_billing",
        "emit_completed",
    ]
    assert fakes.marks() == [
        "finalize_call_record",
        "await_recording",
        "store_recording",
        "finalize_billing",
        "emit_completed",
    ]
    assert fakes.stored_events() == [event]
    result = run.result
    assert (result.status, result.recording, result.price_micros, result.completed) == (
        "completed",
        "stored",
        180_000,
        "emitted",
    )
    assert result.ran == (
        "finalize_call_record",
        "await_recording",
        "store_recording",
        "finalize_billing",
        "emit_completed",
    )
    assert (result.skipped, result.failed, result.timed_out) == ((), (), False)
    assert run.timers() == [POSTCALL_BUDGET_S]
    assert run.phase == "done"
    # The copy may use the budget minus the billing reserve (it starts at once here).
    (window,) = run.schedule_to_close("store_recording")
    assert timedelta(seconds=_STORE_WINDOW_S - 5) < window <= timedelta(seconds=_STORE_WINDOW_S)


async def test_an_unanswered_call_runs_record_billing_and_completion_only(env: WorkflowEnvironment) -> None:
    fakes = FakePostCallActivities(record=facts(connected=False, wanted=True))
    run = await run_post_call(env, fakes)

    assert fakes.names() == ["finalize_call_record", "finalize_billing", "emit_completed"]
    assert run.result.recording == "not_wanted"
    assert run.result.skipped == ()


@pytest.mark.parametrize(
    ("record", "outcome"),
    [(facts(wanted=False), "not_wanted"), (facts(wanted=True, storage=False), "no_storage")],
)
async def test_no_recording_steps_without_a_wanted_recording_or_storage(
    env: WorkflowEnvironment, record: object, outcome: str
) -> None:
    fakes = FakePostCallActivities(record=record)  # type: ignore[arg-type]
    run = await run_post_call(env, fakes)

    assert fakes.names() == ["finalize_call_record", "finalize_billing", "emit_completed"]
    assert run.result.recording == outcome


async def test_a_signal_during_the_lookup_is_stored(env: WorkflowEnvironment) -> None:
    start = postcall_start()
    event = recording_event(start, "evt-signalled")
    fakes = FakePostCallActivities(record=CONNECTED_RECORDED, send={"find_saved_recording": [event]})
    run = await run_post_call(env, fakes, start)

    assert fakes.stored_events() == [event]
    assert run.result.recording == "stored"
    assert run.timers() == [POSTCALL_BUDGET_S]


async def test_a_signal_after_the_lookup_ends_the_wait(env: WorkflowEnvironment) -> None:
    start = postcall_start()
    first = recording_event(start, "evt-first")
    second = recording_event(start, "evt-second")
    fakes = FakePostCallActivities(record=CONNECTED_RECORDED, send={"report_recording_missing": []})
    # The signal goes out right after the start: it lands before or after the lookup, never lost.
    run = await run_post_call(env, fakes, start, signals=[first, second])

    assert fakes.stored_events() == [first]
    assert "report_recording_missing" not in fakes.names()
    assert run.result.recording == "stored"


async def test_no_recording_within_ten_minutes_reports_it_missing(env: WorkflowEnvironment) -> None:
    fakes = FakePostCallActivities(record=CONNECTED_RECORDED)
    run = await run_post_call(env, fakes)

    assert fakes.names() == [
        "finalize_call_record",
        "find_saved_recording",
        "report_recording_missing",
        "finalize_billing",
        "emit_completed",
    ]
    assert run.timers() == [POSTCALL_BUDGET_S, RECORDING_WAIT_S]
    assert fakes.marks() == ["finalize_call_record", "await_recording", "finalize_billing", "emit_completed"]
    result = run.result
    assert (result.recording, result.skipped, result.completed) == (
        "missing",
        ("store_recording",),
        "emitted",
    )


async def test_completed_steps_of_an_earlier_run_are_skipped(env: WorkflowEnvironment) -> None:
    done = (
        "finalize_call_record",
        "await_recording",
        "store_recording",
        "finalize_billing",
        "emit_completed",
    )
    fakes = FakePostCallActivities(record=facts(wanted=True, done=done))
    run = await run_post_call(env, fakes)

    assert fakes.names() == ["finalize_call_record", "emit_completed"]
    assert run.result.skipped == ("await_recording", "store_recording", "finalize_billing")
    assert run.result.recording == "skipped"
    assert run.result.price_micros is None


async def test_a_received_but_unstored_recording_is_looked_up_again(env: WorkflowEnvironment) -> None:
    start = postcall_start()
    event = recording_event(start)
    fakes = FakePostCallActivities(
        record=facts(wanted=True, done=("finalize_call_record", "await_recording")),
        found=[SavedRecording(event=event)],
    )
    run = await run_post_call(env, fakes, start)

    assert fakes.names() == [
        "finalize_call_record",
        "find_saved_recording",
        "store_recording",
        "finalize_billing",
        "emit_completed",
    ]
    assert fakes.stored_events() == [event]
    assert run.result.skipped == ("await_recording",)
    assert run.timers() == [POSTCALL_BUDGET_S]


async def test_a_lost_recording_of_an_earlier_run_skips_the_store(env: WorkflowEnvironment) -> None:
    fakes = FakePostCallActivities(record=facts(wanted=True, done=("await_recording",)))
    run = await run_post_call(env, fakes)

    assert fakes.names() == [
        "finalize_call_record",
        "find_saved_recording",
        "finalize_billing",
        "emit_completed",
    ]
    assert (run.result.recording, run.result.skipped) == ("skipped", ("await_recording", "store_recording"))


async def test_failed_steps_are_listed_and_completion_still_runs(env: WorkflowEnvironment) -> None:
    start = postcall_start()
    fakes = FakePostCallActivities(
        record=CONNECTED_RECORDED,
        found=[SavedRecording(event=recording_event(start))],
        store=boom("store"),
        billing=boom("billing"),
    )
    run = await run_post_call(env, fakes, start)

    assert fakes.names()[-1] == "emit_completed"
    assert run.result.failed == ("store_recording", "finalize_billing")
    assert (run.result.recording, run.result.price_micros, run.result.completed) == (
        "skipped",
        None,
        "emitted",
    )
    assert fakes.marks() == ["finalize_call_record", "await_recording", "emit_completed"]


async def test_an_unusable_recording_is_marked_done(env: WorkflowEnvironment) -> None:
    start = postcall_start()
    fakes = FakePostCallActivities(
        record=CONNECTED_RECORDED,
        found=[SavedRecording(event=recording_event(start))],
        store=StoreRecordingResult(status="failed", detail="invalid_wav"),
    )
    run = await run_post_call(env, fakes, start)

    assert run.result.recording == "failed"
    assert "store_recording" in fakes.marks()


async def test_a_failed_completion_is_reported(env: WorkflowEnvironment) -> None:
    fakes = FakePostCallActivities(completed=boom("emit"))
    run = await run_post_call(env, fakes)

    assert run.result.failed == ("emit_completed",)
    assert run.result.completed is None
    assert "emit_completed" not in fakes.marks()


async def test_a_failed_record_step_still_bills_and_completes(env: WorkflowEnvironment) -> None:
    fakes = FakePostCallActivities(record=boom("record"))
    run = await run_post_call(env, fakes)

    assert fakes.names() == ["finalize_call_record", "finalize_billing", "emit_completed"]
    assert run.result.failed == ("finalize_call_record",)


async def test_a_missing_call_ends_after_the_record_step(env: WorkflowEnvironment) -> None:
    fakes = FakePostCallActivities(record=not_found())
    run = await run_post_call(env, fakes)

    assert fakes.names() == ["finalize_call_record"]
    assert run.result.status == "call_not_found"
    assert run.result.completed is None


async def test_failed_bookkeeping_never_fails_a_step(env: WorkflowEnvironment) -> None:
    fakes = FakePostCallActivities(mark=boom("mark"))
    run = await run_post_call(env, fakes)

    assert run.result.failed == ()
    assert run.result.completed == "emitted"


async def test_the_budget_cancels_the_wait_and_still_completes(env: WorkflowEnvironment) -> None:
    start = postcall_start(PostCallTimings(recording_wait_s=RECORDING_WAIT_S, budget_s=60))
    fakes = FakePostCallActivities(record=CONNECTED_RECORDED)
    run = await run_post_call(env, fakes, start)

    assert fakes.names() == ["finalize_call_record", "find_saved_recording", "emit_completed"]
    result = run.result
    assert (result.timed_out, result.completed, result.price_micros) == (True, "emitted", None)
    assert sorted(run.timers()) == [60, RECORDING_WAIT_S]


BLOCKED_BUDGET_S = 2
"""A blocking fake keeps the time-skipping server on wall time, so these budgets are a few real seconds."""


async def test_the_budget_cancels_running_billing_and_still_completes(env: WorkflowEnvironment) -> None:
    start = postcall_start(PostCallTimings(recording_wait_s=RECORDING_WAIT_S, budget_s=BLOCKED_BUDGET_S))
    fakes = FakePostCallActivities(record=facts(connected=False), block=frozenset({"finalize_billing"}))
    run = await run_post_call(env, fakes, start)

    assert fakes.names() == ["finalize_call_record", "finalize_billing", "emit_completed"]
    assert fakes.cancelled == ["finalize_billing"]
    assert fakes.marks() == ["finalize_call_record", "emit_completed"]
    result = run.result
    assert (result.timed_out, result.completed, result.price_micros, result.failed) == (
        True,
        "emitted",
        None,
        (),
    )
    assert run.timers() == [BLOCKED_BUDGET_S]


async def test_the_budget_cancels_a_running_lookup_without_waiting_for_the_recording(
    env: WorkflowEnvironment,
) -> None:
    start = postcall_start(PostCallTimings(recording_wait_s=RECORDING_WAIT_S, budget_s=BLOCKED_BUDGET_S))
    fakes = FakePostCallActivities(record=CONNECTED_RECORDED, block=frozenset({"find_saved_recording"}))
    run = await run_post_call(env, fakes, start)

    # No recording wait, no missing report, no billing: completion follows the cancelled lookup at once.
    assert fakes.names() == ["finalize_call_record", "find_saved_recording", "emit_completed"]
    assert fakes.cancelled == ["find_saved_recording"]
    assert fakes.marks() == ["finalize_call_record", "emit_completed"]
    result = run.result
    assert (result.timed_out, result.completed, result.price_micros) == (True, "emitted", None)
    assert run.timers() == [BLOCKED_BUDGET_S]


async def test_a_failed_lookup_still_waits_for_the_recording(env: WorkflowEnvironment) -> None:
    fakes = FakePostCallActivities(record=CONNECTED_RECORDED, found=[boom("lookup")])
    run = await run_post_call(env, fakes)

    assert fakes.names() == [
        "finalize_call_record",
        "find_saved_recording",
        "report_recording_missing",
        "finalize_billing",
        "emit_completed",
    ]
    assert (run.result.recording, run.result.failed) == ("missing", ())
    assert RECORDING_WAIT_S in run.timers()


async def test_an_unrecorded_missing_recording_is_not_marked(env: WorkflowEnvironment) -> None:
    fakes = FakePostCallActivities(record=CONNECTED_RECORDED, missing=boom("report"))
    run = await run_post_call(env, fakes)

    assert "report_recording_missing" in fakes.names()
    assert "await_recording" not in fakes.marks()
    assert (run.result.recording, run.result.failed, run.result.completed) == ("missing", (), "emitted")


_STORE_WINDOW_S = POSTCALL_BUDGET_S - int(STORE_RESERVE.total_seconds())
"""The copy's deadline when it starts at once (1275 s)."""
_LONG_BUSY = timedelta(minutes=6, seconds=1)
"""The longest busy delay (copy-lock TTL + margin): retries at 0, 361, 722, 1083 s fit the window, 1444 s
does not."""


async def test_busy_retries_past_the_store_window_fail_the_copy_and_billing_still_runs(
    env: WorkflowEnvironment,
) -> None:
    start = postcall_start()
    event = recording_event(start)
    busy = retryable(BUSY_MESSAGE, RECORDING_BUSY, next_retry_delay=_LONG_BUSY)
    fakes = FakePostCallActivities(record=CONNECTED_RECORDED, found=[SavedRecording(event=event)], store=busy)
    run = await run_post_call(env, fakes, start)

    attempts = [name for name in fakes.names() if name == "store_recording"]
    assert len(attempts) == _STORE_WINDOW_S // int(_LONG_BUSY.total_seconds()) + 1
    assert fakes.names()[-2:] == ["finalize_billing", "emit_completed"]
    assert "store_recording" not in fakes.marks()
    result = run.result
    assert (result.timed_out, result.recording, result.failed, result.price_micros, result.completed) == (
        False,
        "skipped",
        ("store_recording",),
        180_000,
        "emitted",
    )
    assert run.history_span() < timedelta(seconds=POSTCALL_BUDGET_S)


async def test_no_store_window_left_skips_the_copy_and_bills(env: WorkflowEnvironment) -> None:
    start = postcall_start(
        PostCallTimings(recording_wait_s=RECORDING_WAIT_S, budget_s=int(STORE_RESERVE.total_seconds()))
    )
    event = recording_event(start)
    fakes = FakePostCallActivities(record=CONNECTED_RECORDED, found=[SavedRecording(event=event)])
    run = await run_post_call(env, fakes, start)

    assert fakes.names() == [
        "finalize_call_record",
        "find_saved_recording",
        "finalize_billing",
        "emit_completed",
    ]
    assert fakes.marks() == ["finalize_call_record", "await_recording", "finalize_billing", "emit_completed"]
    result = run.result
    assert (result.timed_out, result.recording, result.skipped, result.failed, result.price_micros) == (
        False,
        "skipped",
        ("store_recording",),
        (),
        180_000,
    )
    assert "store_recording" not in result.ran
