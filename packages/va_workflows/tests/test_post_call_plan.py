"""Which post-call steps run (specs/18 §1 Decision P05-T18b)."""

from __future__ import annotations

import pytest
from postcall_rig import facts

from va_domain.post_call import POSTCALL_STEPS
from va_workflows.post_call.plan import ALWAYS_RUN, planned_steps, remaining


def test_the_step_names_follow_the_spec_table() -> None:
    assert POSTCALL_STEPS[0] == "finalize_call_record"
    assert POSTCALL_STEPS[10] == "finalize_billing"
    assert POSTCALL_STEPS[12] == "emit_completed"
    assert len(set(POSTCALL_STEPS)) == 13


@pytest.mark.parametrize(
    ("connected", "wanted", "storage", "expected"),
    [
        (True, True, True, ("await_recording", "store_recording")),
        (True, True, False, ()),
        (True, False, True, ()),
        (False, True, True, ()),
    ],
)
def test_recording_steps_need_a_connected_call_a_wanted_recording_and_storage(
    connected: bool, wanted: bool, storage: bool, expected: tuple[str, ...]
) -> None:
    planned = planned_steps(facts(connected=connected, wanted=wanted, storage=storage))
    assert planned == ("finalize_call_record", *expected, "finalize_billing", "emit_completed")


def test_without_facts_only_billing_and_completion_are_planned() -> None:
    assert planned_steps(None) == ("finalize_billing", "emit_completed")


def test_remaining_drops_completed_steps_but_keeps_the_always_run_ones() -> None:
    planned = planned_steps(facts(wanted=True))
    assert remaining(planned, planned) == ("finalize_call_record", "emit_completed")
    assert remaining(planned, ["await_recording", "unknown"]) == (
        "finalize_call_record",
        "store_recording",
        "finalize_billing",
        "emit_completed",
    )
    assert frozenset({"finalize_call_record", "emit_completed"}) == ALWAYS_RUN
