"""Which ``PostCallWorkflow`` steps run for a call (specs/18-post-call-analysis.md §1, Decision P05-T18b).

Pure functions over :class:`CallRecordFacts`; workflow-safe (no I/O imports).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from types import MappingProxyType
from typing import Final

from va_domain.post_call import POSTCALL_STEPS, PostCallStep
from va_workflows.activities.post_call_inputs import CallRecordFacts

__all__ = ["ALWAYS_RUN", "IMPLEMENTED", "planned_steps", "remaining"]

ALWAYS_RUN: frozenset[PostCallStep] = frozenset({"finalize_call_record", "emit_completed"})
"""Steps that run on every execution: step 1 returns the facts the rest is planned with, and step 13 is the
end of every run (both are guarded, so a re-run changes nothing)."""


def _always(_facts: CallRecordFacts | None) -> bool:
    return True


def _with_facts(facts: CallRecordFacts | None) -> bool:
    return facts is not None


def _recorded(facts: CallRecordFacts | None) -> bool:
    return facts is not None and facts.connected and facts.recording_wanted and facts.recording_storage


_PLAN: Final[Mapping[PostCallStep, Callable[[CallRecordFacts | None], bool]]] = MappingProxyType(
    {
        "finalize_call_record": _with_facts,
        "await_recording": _recorded,
        "store_recording": _recorded,
        "finalize_billing": _always,
        "emit_completed": _always,
    }
)
"""The one table of plannable steps and the condition each runs under. A new step joins here, and when it
runs between ``store_recording`` and ``emit_completed`` it also needs a ``budget.STEP_WORST_CASE`` entry
(specs/18 §1 Decisions P08-FIX-8, P08-FIX-9)."""

IMPLEMENTED: Final[frozenset[PostCallStep]] = frozenset(_PLAN)
"""The steps :func:`planned_steps` can plan, derived from the plan table (never edited by hand)."""


def planned_steps(facts: CallRecordFacts | None) -> tuple[PostCallStep, ...]:
    """The implemented steps that apply to the call, in spec order.

    Every call: 1, 11, 13. A connected call whose recording is wanted also gets 2 and 3 when the worker has
    object storage. ``None`` (step 1 failed) plans 11 and 13 only, which check the call themselves.
    """
    return tuple(step for step in POSTCALL_STEPS if (runs := _PLAN.get(step)) is not None and runs(facts))


def remaining(planned: Iterable[PostCallStep], done: Iterable[str]) -> tuple[PostCallStep, ...]:
    """``planned`` without the steps an earlier run completed (``ALWAYS_RUN`` steps stay)."""
    completed = set(done)
    return tuple(step for step in planned if step in ALWAYS_RUN or step not in completed)
