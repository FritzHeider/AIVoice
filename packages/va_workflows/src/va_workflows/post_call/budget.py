"""How much of the post-call budget ``store_recording`` may use (specs/18-post-call-analysis.md §1, Decision
P08-FIX-6).

Pure functions over the activities' timeouts and retry policies; workflow-safe (no I/O imports). The copy is
the only step whose retries can wait for minutes (a busy copy lock asks for a retry after the holder's lock
TTL), so it gets a ``schedule_to_close`` deadline that leaves room for the steps after it: every implemented
step between the copy and ``emit_completed`` plus the step marks, each at its policy's worst case (Decision
P08-FIX-8 derives the reserve from :data:`va_workflows.post_call.plan.IMPLEMENTED`). Importing the module
fails when the slowest path to the copy, the minimum window, and the reserve no longer fit in the default
budget (Decision P08-FIX-9).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Final

from temporalio.common import RetryPolicy

from va_domain.post_call import POSTCALL_BUDGET_S, POSTCALL_STEPS, RECORDING_WAIT_S, PostCallStep
from va_workflows.activities.post_call_inputs import (
    BILLING_RETRY,
    BILLING_TIMEOUT,
    BOOKKEEPING_RETRY,
    BOOKKEEPING_TIMEOUT,
    RECORD_RETRY,
    RECORD_TIMEOUT,
)
from va_workflows.post_call.plan import IMPLEMENTED

__all__ = [
    "MARK_WORST_CASE",
    "MIN_STORE_WINDOW",
    "PRE_STORE_WORST_CASE",
    "STEP_WORST_CASE",
    "STORE_RESERVE",
    "check_copy_window",
    "reserve_for",
    "retry_worst_case",
    "steps_after_store",
    "store_window",
]

_DEFAULT_MAXIMUM_FACTOR: Final = 100
"""Temporal's default ``maximum_interval`` is 100 x ``initial_interval``."""

MIN_STORE_WINDOW: Final = timedelta(seconds=60)
"""A window shorter than this is no window: a copy that cannot realistically finish is skipped instead of
started (Decision P08-FIX-7); an attempt that still overruns stops itself at ``commit_by``."""


def retry_worst_case(timeout: timedelta, policy: RetryPolicy) -> timedelta:
    """The longest an activity with ``start_to_close=timeout`` and ``policy`` can take: every attempt runs
    into its timeout and every backoff interval (``initial x coefficient^i``, capped at ``maximum_interval``)
    is waited in full. Raises ``ValueError`` for an unlimited policy (``maximum_attempts < 1``)."""
    attempts = policy.maximum_attempts
    if attempts < 1:
        message = "an unlimited retry policy has no worst case"
        raise ValueError(message)
    cap = policy.maximum_interval or policy.initial_interval * _DEFAULT_MAXIMUM_FACTOR
    backoff = sum(
        (min(policy.initial_interval * policy.backoff_coefficient**i, cap) for i in range(attempts - 1)),
        timedelta(),
    )
    return attempts * timeout + backoff


MARK_WORST_CASE: Final = retry_worst_case(BOOKKEEPING_TIMEOUT, BOOKKEEPING_RETRY)
"""The worst case of one step mark (``mark_postcall_step``)."""

STEP_WORST_CASE: Final[Mapping[PostCallStep, timedelta]] = MappingProxyType(
    {"finalize_billing": retry_worst_case(BILLING_TIMEOUT, BILLING_RETRY)}
)
"""The worst case of every step that may run after the copy. Each implemented step between
``store_recording`` and ``emit_completed`` needs an entry (:func:`reserve_for` refuses a step without one);
``emit_completed`` runs whatever is left of the budget, so it has none."""


def steps_after_store(implemented: Iterable[PostCallStep] = IMPLEMENTED) -> tuple[PostCallStep, ...]:
    """The steps of ``implemented`` strictly between ``store_recording`` and ``emit_completed``, in spec
    order."""
    wanted = set(implemented)
    start = POSTCALL_STEPS.index("store_recording") + 1
    end = POSTCALL_STEPS.index("emit_completed")
    return tuple(step for step in POSTCALL_STEPS[start:end] if step in wanted)


def reserve_for(
    steps: Iterable[PostCallStep], *, worst_cases: Mapping[PostCallStep, timedelta] = STEP_WORST_CASE
) -> timedelta:
    """The budget that must remain once the copy is over: the copy's own mark plus, for each of ``steps``,
    its worst case and its mark. Raises ``ValueError`` for a step without a worst case."""
    reserve = MARK_WORST_CASE
    for step in steps:
        worst = worst_cases.get(step)
        if worst is None:
            message = f"no worst case for post-call step {step}"
            raise ValueError(message)
        reserve += worst + MARK_WORST_CASE
    return reserve


STORE_RESERVE: Final = reserve_for(steps_after_store())
"""What must stay of the budget once the copy is over (today ``finalize_billing`` and the marks of
``store_recording`` and ``finalize_billing``: 365 s + 2 x 80 s = 525 s). It excludes schedule-to-start
latency: a worker backlog can still push the later steps past the budget, and the budget timer then ends
the run and ``emit_completed`` reports it, as for any overrun. The worst cases are summed because the
steps after the copy run one after another; steps run in parallel would reserve their maximum instead."""

PRE_STORE_WORST_CASE: Final = (
    retry_worst_case(RECORD_TIMEOUT, RECORD_RETRY) + 3 * MARK_WORST_CASE + timedelta(seconds=RECORDING_WAIT_S)
)
"""The slowest path to the copy: step 1, its mark, the recording lookup (bookkeeping policy), the full
recording wait, and the wait's mark, each at its worst case (today 1005 s)."""


def check_copy_window(*, before_store: timedelta, reserve: timedelta, budget_s: int) -> None:
    """Raise ``ValueError`` when ``before_store`` + ``MIN_STORE_WINDOW`` + ``reserve`` exceeds the budget,
    i.e. when the slowest run would always skip the copy."""
    if before_store + MIN_STORE_WINDOW + reserve > timedelta(seconds=budget_s):
        message = (
            f"the post-call budget of {budget_s} s leaves no store_recording window: {before_store} before "
            f"the copy, {reserve} reserved after it"
        )
        raise ValueError(message)


check_copy_window(before_store=PRE_STORE_WORST_CASE, reserve=STORE_RESERVE, budget_s=POSTCALL_BUDGET_S)


def store_window(started: datetime, now: datetime, budget_s: int) -> timedelta | None:
    """The time ``store_recording`` may use from ``now`` (all attempts and retry waits together), ``None``
    when less than ``MIN_STORE_WINDOW`` is left before ``started + budget - STORE_RESERVE``."""
    left = started + timedelta(seconds=budget_s) - STORE_RESERVE - now
    return left if left >= MIN_STORE_WINDOW else None
