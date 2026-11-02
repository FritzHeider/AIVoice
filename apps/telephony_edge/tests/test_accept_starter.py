"""``TemporalInboundStarter`` over a fake Temporal client (specs/06 §7 Decision P05-T12c)."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from structlog.testing import capture_logs
from temporalio.exceptions import WorkflowAlreadyStartedError
from workflow_fakes import SignalRecorder, connector_of

from va_common.checks import WorkflowStartError
from va_domain.call_workflow import CALL_WORKFLOW_NAME, CallWorkflowInput
from va_edge.accept import START_RPC_TIMEOUT_S, TemporalInboundStarter

CALL = CallWorkflowInput(call_id=uuid.uuid4(), org_id=uuid.uuid4(), direction="inbound", max_duration_s=600)


async def test_the_inbound_input_is_started_on_the_task_queue() -> None:
    client = SignalRecorder()
    await TemporalInboundStarter(connector_of(client), "va-main").start(CALL)
    [(name, arg, options)] = client.started
    assert (name, arg) == (CALL_WORKFLOW_NAME, CALL.model_dump(mode="json"))
    assert (options["id"], options["task_queue"]) == (f"call-{CALL.call_id}", "va-main")
    assert options["rpc_timeout"] == timedelta(seconds=START_RPC_TIMEOUT_S)


async def test_an_existing_workflow_is_logged_and_counts_as_started() -> None:
    client = SignalRecorder()
    client.start_error = WorkflowAlreadyStartedError(f"call-{CALL.call_id}", CALL_WORKFLOW_NAME)
    with capture_logs() as logs:
        await TemporalInboundStarter(connector_of(client), "va-main").start(CALL)
    assert "telnyx.inbound_workflow_already_started" in [log["event"] for log in logs]


async def test_a_failed_start_raises() -> None:
    client = SignalRecorder()
    client.start_error = OSError("refused")
    with pytest.raises(WorkflowStartError):
        await TemporalInboundStarter(connector_of(client), "va-main").start(CALL)


async def test_terminate_targets_the_call_workflow() -> None:
    client = SignalRecorder()
    await TemporalInboundStarter(connector_of(client), "va-main").terminate(
        CALL.call_id, "inbound_accept_failed"
    )
    assert client.terminated == [(f"call-{CALL.call_id}", "inbound_accept_failed")]
