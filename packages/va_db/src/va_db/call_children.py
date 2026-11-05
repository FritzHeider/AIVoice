"""Child calls created from a parent call: voicemail retries (specs/07 §14 Decision P05-T10f) and callbacks of
the ``schedule_callback`` tool (specs/13 §6 Decision P08-T02b2).

A child is an ordinary **scheduled** call: status ``scheduled``, ``parent_call_id`` set, no credit hold (the
child's ``dial`` admits it like every scheduled call), its own ``CallWorkflow`` id, and the dispatch columns
of the parent (:data:`COPIED_COLUMNS`). A callback of an inbound call is an outbound call to the caller from
the number that was called (``outbound=True``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from pydantic import JsonValue

from va_db.models import Call
from va_domain.call_workflow import call_workflow_id

__all__ = ["COPIED_COLUMNS", "child_call"]

COPIED_COLUMNS: Final = (
    "direction",
    "to_number",
    "from_number",
    "runner_type",
    "pathway_id",
    "pathway_version",
    "persona_id",
    "persona_version",
    "agent_id",
    "agent_version_id",
    "experiment_id",
    "experiment_arm_id",
    "voice_id",
    "phone_number_id",
    "memory_id",
    "external_id",
    "test_call",
    "request_data",
    "metadata_",
)
"""``calls`` columns a child takes over from the parent unchanged (the ones Send Call dispatch sets)."""


def child_call(
    parent: Call,
    *,
    child_id: uuid.UUID,
    config: dict[str, JsonValue],
    start_time: datetime,
    outbound: bool = False,
) -> Call:
    """The scheduled child of ``parent`` (not added to a session). With ``outbound`` an inbound parent's child
    calls the parent's caller (``to``/``from`` swapped, ``direction="outbound"``)."""
    copied = {name: getattr(parent, name) for name in COPIED_COLUMNS}
    if outbound and parent.direction == "inbound":
        copied.update(direction="outbound", to_number=parent.from_number, from_number=parent.to_number)
    return Call(
        id=child_id,
        org_id=parent.org_id,
        status="scheduled",
        queue_status="new",
        start_time=start_time,
        parent_call_id=parent.id,
        config=config,
        credit_hold_id=None,
        workflow_id=call_workflow_id(child_id),
        **copied,
    )
