"""``TemporalPipelineSignaller`` (specs/07 §18 step 5; specs/07 §18 Decision P05-T11b)."""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from service_helpers import json_log_stream, log_events
from temporalio.client import Client
from temporalio.converter import DataConverter
from temporalio.service import RPCError, RPCStatusCode

from va_common.checks import TemporalConnector
from va_domain.call_workflow import PIPELINE_FINISHED_SIGNAL, PipelineFinished
from va_media.workflow_signal import TemporalPipelineSignaller

CALL_ID = uuid.UUID("0190f5c2-0000-7000-8000-0000000000b1")
FINISHED = PipelineFinished(end_reason="agent_end_call", ended_by="agent")


class Workflows:
    """A stand-in Temporal client: records ``(workflow id, signal, arg)`` and raises ``error`` after it."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.sent: list[tuple[str, str, Any]] = []
        self._workflow_id = ""

    def get_workflow_handle(self, workflow_id: str) -> Workflows:
        self._workflow_id = workflow_id
        return self

    async def signal(self, name: str, arg: Any) -> None:
        self.sent.append((self._workflow_id, name, arg))
        if self.error is not None:
            raise self.error


def signaller(client: Workflows | None, connect_error: Exception | None = None) -> TemporalPipelineSignaller:
    async def connect(_host: str, _namespace: str, _converter: DataConverter | None) -> Client:
        if connect_error is not None:
            raise connect_error
        return cast("Client", client)

    return TemporalPipelineSignaller(TemporalConnector("temporal.test:7233", "default", connect=connect))


async def test_signals_the_call_workflow_with_the_json_summary() -> None:
    stream = json_log_stream()
    client = Workflows()
    await signaller(client).pipeline_finished(CALL_ID, FINISHED)
    assert client.sent == [
        (f"call-{CALL_ID}", PIPELINE_FINISHED_SIGNAL, {"end_reason": "agent_end_call", "ended_by": "agent"})
    ]
    assert "media.pipeline_finished_signalled" in {e["event"] for e in log_events(stream)}


async def test_a_missing_workflow_is_not_an_error() -> None:
    stream = json_log_stream()
    client = Workflows(RPCError("workflow not found", RPCStatusCode.NOT_FOUND, b""))
    await signaller(client).pipeline_finished(CALL_ID, FINISHED)
    events = [e["event"] for e in log_events(stream)]
    assert "media.workflow_missing" in events
    assert "media.signal_failed" not in events


@pytest.mark.parametrize(
    ("rpc_error", "connect_error"),
    [
        (RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b""), None),
        (None, RuntimeError("connection refused")),
        (None, OSError("unreachable")),
    ],
    ids=["rpc", "connect_runtime", "connect_os"],
)
async def test_other_failures_are_logged_never_raised(
    rpc_error: Exception | None, connect_error: Exception | None
) -> None:
    stream = json_log_stream()
    await signaller(Workflows(rpc_error), connect_error).pipeline_finished(CALL_ID, FINISHED)
    failed = [e for e in log_events(stream) if e["event"] == "media.signal_failed"]
    assert len(failed) == 1
    assert failed[0]["end_reason"] == "agent_end_call"
