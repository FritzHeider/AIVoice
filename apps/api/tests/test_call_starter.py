"""``TemporalCallStarter`` (specs/05 §5 Decision P05-T05b) over a fake Temporal client."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from structlog.testing import capture_logs
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from va_api.services.call_starter import START_RPC_TIMEOUT_S, CallStartError, TemporalCallStarter
from va_common.checks import TemporalConnector
from va_domain.call_workflow import CallWorkflowInput

CALL = CallWorkflowInput(
    call_id=uuid.UUID("0190f5c2-0000-7000-8000-00000000000a"),
    org_id=uuid.UUID(int=3),
    scheduled_for=datetime(2026, 11, 2, 9, tzinfo=UTC),
)


@dataclass
class FakeClient:
    error: BaseException | None = None
    hang: bool = False
    starts: list[tuple[tuple[Any, ...], dict[str, Any]]] = field(default_factory=list)

    async def start_workflow(self, *args: Any, **kwargs: Any) -> None:
        self.starts.append((args, kwargs))
        if self.hang:
            await asyncio.Event().wait()
        if self.error is not None:
            raise self.error


def starter(
    client: FakeClient, *, connect_error: Exception | None = None, timeout_s: float = 10.0
) -> TemporalCallStarter:
    async def connect(_host: str, _namespace: str) -> Client:
        if connect_error is not None:
            raise connect_error
        return cast("Client", client)

    connector = TemporalConnector("temporal.test:7233", "default", connect=connect)
    return TemporalCallStarter(connector, "va-main", start_timeout_s=timeout_s)


async def test_starts_call_workflow_with_the_json_input() -> None:
    client = FakeClient()
    await starter(client).start(CALL)
    assert client.starts == [
        (
            ("CallWorkflow", CALL.model_dump(mode="json")),
            {
                "id": "call-0190f5c2-0000-7000-8000-00000000000a",
                "task_queue": "va-main",
                "id_reuse_policy": WorkflowIDReusePolicy.REJECT_DUPLICATE,
                "rpc_timeout": timedelta(seconds=START_RPC_TIMEOUT_S),
            },
        )
    ]


async def test_an_already_started_workflow_counts_as_started() -> None:
    client = FakeClient(WorkflowAlreadyStartedError("call-x", "CallWorkflow"))
    with capture_logs() as logs:
        await starter(client).start(CALL)
    assert [entry["event"] for entry in logs] == ["calls.workflow_already_started"]


@pytest.mark.parametrize(
    "error",
    [RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b""), OSError("refused")],
)
async def test_start_failures_raise_call_start_error(error: Exception) -> None:
    with pytest.raises(CallStartError) as caught:
        await starter(FakeClient(error)).start(CALL)
    assert caught.value.__cause__ is error


async def test_a_failed_connect_raises_call_start_error() -> None:
    error = RuntimeError("Failed client connect")
    client = FakeClient()
    with pytest.raises(CallStartError) as caught:
        await starter(client, connect_error=error).start(CALL)
    assert caught.value.__cause__ is error
    assert client.starts == []


async def test_a_hanging_start_times_out() -> None:
    client = FakeClient(hang=True)
    with pytest.raises(CallStartError) as caught:
        await starter(client, timeout_s=0.01).start(CALL)
    assert isinstance(caught.value.__cause__, TimeoutError)
