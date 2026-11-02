"""``va_db.call_rows.call_row``: the new ``calls`` row shared by Send Call dispatch and the inbound accept."""

from __future__ import annotations

import uuid

from resolved_configs import resolved_config

from va_db.call_rows import NewCall, call_row
from va_domain.calls import SendCallConfig


def test_the_row_takes_the_config_columns_and_the_creator_facts() -> None:
    config = SendCallConfig.model_validate(resolved_config())
    call_id, org_id, hold_id, contact_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    new = NewCall(
        direction="inbound",
        status="ringing",
        queue_status="started",
        to_number="+14155550100",
        from_number="+14155550123",
        test_call=True,
        credit_hold_id=hold_id,
        contact_id=contact_id,
    )

    row = call_row(call_id, org_id, config, new, {"pricing": {"connected_rate_micros_per_min": 1}})

    assert (row.id, row.org_id, row.workflow_id) == (call_id, org_id, f"call-{call_id}")
    assert (row.direction, row.status, row.queue_status) == ("inbound", "ringing", "started")
    assert (row.to_number, row.from_number, row.test_call) == ("+14155550100", "+14155550123", True)
    assert (row.credit_hold_id, row.contact_id) == (hold_id, contact_id)
    assert (row.runner_type, row.voice_id, row.phone_number_id) == (
        config.runner_type,
        config.voice_id,
        config.phone_number_id,
    )
    assert (row.request_data, row.metadata_, row.start_time) == (
        config.request_data,
        config.metadata,
        config.start_time,
    )
    assert row.config == {**config.model_dump(mode="json"), "pricing": {"connected_rate_micros_per_min": 1}}


def test_defaults_leave_the_optional_facts_empty() -> None:
    config = SendCallConfig.model_validate(resolved_config())
    new = NewCall(
        direction="outbound", status="queued", queue_status="queued", to_number=None, from_number=None
    )

    row = call_row(uuid.uuid4(), uuid.uuid4(), config, new, {})

    assert (row.test_call, row.credit_hold_id, row.contact_id) == (False, None, None)
    assert row.config == config.model_dump(mode="json")
