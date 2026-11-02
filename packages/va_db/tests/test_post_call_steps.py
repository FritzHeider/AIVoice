"""``calls.config.postcall_steps`` reading (specs/18 §1 Decision P05-T18b); the guarded append runs against
Postgres in ``packages/va_workflows/tests/test_post_call_activities.py``."""

from __future__ import annotations

from typing import Any

import pytest

from va_db.post_call_steps import done_steps


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (None, ()),
        ({}, ()),
        ({"postcall_steps": "finalize_billing"}, ()),
        (
            {"postcall_steps": ["finalize_call_record", 3, "emit_completed"]},
            ("finalize_call_record", "emit_completed"),
        ),
    ],
)
def test_done_steps_reads_the_string_entries_in_order(
    config: dict[str, Any] | None, expected: tuple[str, ...]
) -> None:
    assert done_steps(config) == expected
