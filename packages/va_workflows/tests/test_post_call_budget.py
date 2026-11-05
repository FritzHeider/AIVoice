"""The ``store_recording`` window inside the post-call budget (specs/18 §1 Decision P08-FIX-6)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from temporalio.common import RetryPolicy

from va_domain.post_call import POSTCALL_BUDGET_S, RECORDING_WAIT_S
from va_workflows.activities.post_call_inputs import (
    BILLING_RETRY,
    BILLING_TIMEOUT,
    BOOKKEEPING_RETRY,
    BOOKKEEPING_TIMEOUT,
    RECORD_RETRY,
    RECORD_TIMEOUT,
)
from va_workflows.post_call.budget import (
    MARK_WORST_CASE,
    MIN_STORE_WINDOW,
    PRE_STORE_WORST_CASE,
    STEP_WORST_CASE,
    STORE_RESERVE,
    check_copy_window,
    reserve_for,
    retry_worst_case,
    steps_after_store,
    store_window,
)
from va_workflows.post_call.plan import IMPLEMENTED

STARTED = datetime(2026, 11, 4, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("timeout", "policy", "seconds"),
    [
        (RECORD_TIMEOUT, RECORD_RETRY, 5 * 30 + (1 + 2 + 4 + 8)),
        (BOOKKEEPING_TIMEOUT, BOOKKEEPING_RETRY, 5 * 10 + (2 + 4 + 8 + 16)),
        (BILLING_TIMEOUT, BILLING_RETRY, 10 * 30 + (1 + 2 + 4 + 8 + 5 * 10)),
    ],
)
def test_the_worst_case_of_each_policy(timeout: timedelta, policy: RetryPolicy, seconds: int) -> None:
    assert retry_worst_case(timeout, policy) == timedelta(seconds=seconds)


def test_one_attempt_is_the_timeout_alone() -> None:
    policy = RetryPolicy(maximum_attempts=1)
    assert retry_worst_case(timedelta(seconds=7), policy) == timedelta(seconds=7)


def test_the_backoff_cap_defaults_to_a_hundred_initial_intervals() -> None:
    policy = RetryPolicy(initial_interval=timedelta(seconds=1), backoff_coefficient=10.0, maximum_attempts=5)
    # 1 s, 10 s, 100 s, then 1000 s capped at 100 x 1 s.
    assert retry_worst_case(timedelta(seconds=1), policy) == timedelta(seconds=5 + 1 + 10 + 100 + 100)


def test_an_unlimited_policy_has_no_worst_case() -> None:
    with pytest.raises(ValueError, match="unlimited"):
        retry_worst_case(timedelta(seconds=1), RetryPolicy(maximum_attempts=0))


def test_the_reserve_covers_billing_and_two_marks() -> None:
    assert timedelta(seconds=525) == STORE_RESERVE


def test_steps_after_the_copy_are_billing_today() -> None:
    assert steps_after_store() == ("finalize_billing",)


def test_every_step_after_the_copy_has_a_worst_case() -> None:
    for step in steps_after_store(IMPLEMENTED):
        assert step in STEP_WORST_CASE


def test_a_mark_is_the_bookkeeping_worst_case() -> None:
    assert timedelta(seconds=80) == MARK_WORST_CASE


def test_a_new_step_without_a_worst_case_is_refused() -> None:
    with pytest.raises(ValueError, match="summarize"):
        reserve_for(steps_after_store(IMPLEMENTED | {"summarize"}))


def test_steps_outside_the_copy_and_completion_do_not_count() -> None:
    assert steps_after_store(("finalize_call_record", "store_recording", "emit_completed")) == ()
    assert reserve_for(()) == MARK_WORST_CASE


def test_a_new_step_grows_the_reserve() -> None:
    worst_cases = {**STEP_WORST_CASE, "summarize": timedelta(seconds=40)}
    reserve = reserve_for(steps_after_store(IMPLEMENTED | {"summarize"}), worst_cases=worst_cases)
    assert reserve == timedelta(seconds=525 + 40 + 80)


def test_the_window_runs_to_the_budget_minus_the_reserve() -> None:
    assert (
        store_window(STARTED, STARTED, POSTCALL_BUDGET_S)
        == timedelta(seconds=POSTCALL_BUDGET_S) - STORE_RESERVE
    )
    later = STARTED + timedelta(seconds=1000)
    assert store_window(STARTED, later, POSTCALL_BUDGET_S) == timedelta(
        seconds=POSTCALL_BUDGET_S - 1000 - 525
    )


def test_no_window_at_or_after_the_deadline() -> None:
    deadline = STARTED + timedelta(seconds=POSTCALL_BUDGET_S) - STORE_RESERVE
    assert store_window(STARTED, deadline, POSTCALL_BUDGET_S) is None
    assert store_window(STARTED, deadline - MIN_STORE_WINDOW / 2, POSTCALL_BUDGET_S) is None
    assert store_window(STARTED, deadline - MIN_STORE_WINDOW, POSTCALL_BUDGET_S) == MIN_STORE_WINDOW
    assert store_window(STARTED, deadline + timedelta(seconds=5), POSTCALL_BUDGET_S) is None


def test_a_copy_needs_a_minute() -> None:
    assert timedelta(seconds=60) == MIN_STORE_WINDOW


def test_the_pre_store_worst_case() -> None:
    """Step 1, its mark, the recording lookup, the full recording wait, and the wait's mark."""
    expected = (
        retry_worst_case(RECORD_TIMEOUT, RECORD_RETRY)
        + 3 * retry_worst_case(BOOKKEEPING_TIMEOUT, BOOKKEEPING_RETRY)
        + timedelta(seconds=RECORDING_WAIT_S)
    )
    assert expected == PRE_STORE_WORST_CASE
    assert timedelta(seconds=1005) == PRE_STORE_WORST_CASE


def test_the_slowest_path_to_the_copy_still_leaves_a_window() -> None:
    window = store_window(STARTED, STARTED + PRE_STORE_WORST_CASE, POSTCALL_BUDGET_S)
    assert window is not None
    assert window == timedelta(seconds=1800 - 1005 - 525)
    assert window >= MIN_STORE_WINDOW


def test_the_default_budget_keeps_a_copy_window() -> None:
    check_copy_window(before_store=PRE_STORE_WORST_CASE, reserve=STORE_RESERVE, budget_s=POSTCALL_BUDGET_S)


def test_a_reserve_that_eats_the_window_is_refused() -> None:
    edge = timedelta(seconds=POSTCALL_BUDGET_S) - PRE_STORE_WORST_CASE - MIN_STORE_WINDOW
    check_copy_window(before_store=PRE_STORE_WORST_CASE, reserve=edge, budget_s=POSTCALL_BUDGET_S)
    with pytest.raises(ValueError, match="no store_recording window"):
        check_copy_window(
            before_store=PRE_STORE_WORST_CASE,
            reserve=edge + timedelta(seconds=1),
            budget_s=POSTCALL_BUDGET_S,
        )


def test_a_short_budget_is_refused() -> None:
    with pytest.raises(ValueError, match="budget of 1500 s"):
        check_copy_window(before_store=PRE_STORE_WORST_CASE, reserve=STORE_RESERVE, budget_s=1500)
