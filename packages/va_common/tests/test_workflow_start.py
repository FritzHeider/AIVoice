"""``va_common.checks.start_workflow`` / ``terminate_workflow`` over a fake Temporal client."""

from __future__ import annotations

from datetime import timedelta

import pytest
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode
from workflow_fakes import SignalRecorder, connector_of

from va_common.checks import WorkflowStartError, start_workflow, terminate_workflow


async def start(
    client: SignalRecorder, *, timeout_s: float = 5.0, connect_error: Exception | None = None
) -> bool:
    return await start_workflow(
        connector_of(client, connect_error),
        "CallWorkflow",
        {"call_id": "c"},
        workflow_id="call-c",
        task_queue="va-main",
        timeout_s=timeout_s,
        rpc_timeout_s=0.8,
    )


async def test_a_started_workflow_uses_reject_duplicate_and_the_rpc_timeout() -> None:
    client = SignalRecorder()
    assert await start(client)
    assert client.started == [
        (
            "CallWorkflow",
            {"call_id": "c"},
            {
                "id": "call-c",
                "task_queue": "va-main",
                "id_reuse_policy": WorkflowIDReusePolicy.REJECT_DUPLICATE,
                "rpc_timeout": timedelta(seconds=0.8),
            },
        )
    ]


async def test_an_existing_workflow_counts_as_started_but_reports_false() -> None:
    client = SignalRecorder()
    client.start_error = WorkflowAlreadyStartedError("call-c", "CallWorkflow")
    assert await start(client) is False


@pytest.mark.parametrize(
    "error", [RPCError("boom", RPCStatusCode.UNAVAILABLE, b""), OSError("refused"), RuntimeError("closed")]
)
async def test_start_failures_raise_workflow_start_error(error: Exception) -> None:
    client = SignalRecorder()
    client.start_error = error
    with pytest.raises(WorkflowStartError) as raised:
        await start(client)
    assert raised.value.__cause__ is error


async def test_a_connect_failure_raises_workflow_start_error() -> None:
    with pytest.raises(WorkflowStartError):
        await start(SignalRecorder(), connect_error=OSError("refused"))


async def test_a_start_slower_than_the_deadline_times_out() -> None:
    client = SignalRecorder()
    client.start_hang = True
    with pytest.raises(WorkflowStartError) as raised:
        await start(client, timeout_s=0.01)
    assert isinstance(raised.value.__cause__, TimeoutError)


async def test_terminate_records_the_reason() -> None:
    client = SignalRecorder()
    assert await terminate_workflow(connector_of(client), "call-c", "inbound_accept_failed")
    assert client.terminated == [("call-c", "inbound_accept_failed")]


async def test_terminating_a_missing_workflow_returns_false() -> None:
    client = SignalRecorder()
    client.terminate_error = RPCError("gone", RPCStatusCode.NOT_FOUND, b"")
    assert await terminate_workflow(connector_of(client), "call-c", "r") is False


async def test_other_terminate_errors_propagate() -> None:
    client = SignalRecorder()
    client.terminate_error = RPCError("down", RPCStatusCode.UNAVAILABLE, b"")
    with pytest.raises(RPCError):
        await terminate_workflow(connector_of(client), "call-c", "r")
