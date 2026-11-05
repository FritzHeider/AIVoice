"""Which ``PostCallWorkflow`` steps run for a call (specs/18-post-call-analysis.md §1, Decision P05-T18b).

Pure functions over :class:`CallRecordFacts`; workflow-safe (no I/O imports).
"""

from __future__ import annotations

from collections.abc import Iterable

from va_domain.post_call import POSTCALL_STEPS, PostCallStep
from va_workflows.activities.post_call_inputs import CallRecordFacts

__all__ = ["ALWAYS_RUN", "IMPLEMENTED", "planned_steps", "remaining"]

ALWAYS_RUN: frozenset[PostCallStep] = frozenset({"finalize_call_record", "emit_completed"})
"""Steps that run on every execution: step 1 returns the facts the rest is planned with, and step 13 is the
end of every run (both are guarded, so a re-run changes nothing)."""

IMPLEMENTED: frozenset[PostCallStep] = frozenset(
    {"finalize_call_record", "await_recording", "store_recording", "finalize_billing", "emit_completed"}
)
"""The steps :func:`planned_steps` can plan. A new step joins here, and when it runs between
``store_recording`` and ``emit_completed`` it also needs a ``budget.STEP_WORST_CASE`` entry (specs/18 §1
Decision P08-FIX-8)."""


def planned_steps(facts: CallRecordFacts | None) -> tuple[PostCallStep, ...]:
    """The implemented steps that apply to the call, in spec order.

    Every call: 1, 11, 13. A connected call whose recording is wanted also gets 2 and 3 when the worker has
    object storage. ``None`` (step 1 failed) plans 11 and 13 only, which check the call themselves.
    """
    wanted: set[PostCallStep] = {"finalize_billing", "emit_completed"}
    if facts is not None:
        wanted.add("finalize_call_record")
        if facts.connected and facts.recording_wanted and facts.recording_storage:
            wanted.update(("await_recording", "store_recording"))
    return tuple(step for step in POSTCALL_STEPS if step in wanted)


def remaining(planned: Iterable[PostCallStep], done: Iterable[str]) -> tuple[PostCallStep, ...]:
    """``planned`` without the steps an earlier run completed (``ALWAYS_RUN`` steps stay)."""
    completed = set(done)
    return tuple(step for step in planned if step in ALWAYS_RUN or step not in completed)
