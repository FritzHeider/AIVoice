"""``TemporalCallbackStarter`` over a stand-in Temporal client (specs/13 §6 Decisions P08-T02b2,
P08-FIX-13)."""

from __future__ import annotations

import pytest
from service_helpers import json_log_stream, log_events
from temporalio.service import RPCError, RPCStatusCode
from workflow_fakes import SignalRecorder, connector_of

from va_common.ids import new_id
from va_domain.call_workflow import CALL_WORKFLOW_NAME, CallWorkflowInput, call_workflow_id
from va_media.call_system_tools import CALLBACK_START_FAILED, CallbackStartError, TemporalCallbackStarter


async def test_start_runs_the_call_workflow_on_the_queue() -> None:
    client = SignalRecorder()
    call = CallWorkflowInput(call_id=new_id(), org_id=new_id())
    await TemporalCallbackStarter(connector_of(client), "calls").start(call)
    [(name, _arg, options)] = client.started
    assert (name, options["id"], options["task_queue"]) == (CALL_WORKFLOW_NAME, call.workflow_id(), "calls")


async def test_a_refused_start_raises_callback_start_error() -> None:
    client = SignalRecorder()
    client.start_error = RPCError("down", RPCStatusCode.UNAVAILABLE, b"")
    with pytest.raises(CallbackStartError):
        await TemporalCallbackStarter(connector_of(client), "calls").start(
            CallWorkflowInput(call_id=new_id(), org_id=new_id())
        )


async def test_abandon_terminates_the_callback_workflow() -> None:
    client = SignalRecorder()
    call_id = new_id()
    assert await TemporalCallbackStarter(connector_of(client), "calls").abandon(call_id)
    assert client.terminated == [(call_workflow_id(call_id), CALLBACK_START_FAILED)]


@pytest.mark.parametrize(
    "error",
    [RPCError("missing", RPCStatusCode.NOT_FOUND, b""), None],
)
async def test_abandon_reports_whether_a_workflow_was_terminated(error: RPCError | None) -> None:
    stream = json_log_stream()
    client = SignalRecorder()
    client.terminate_error = error
    terminated = await TemporalCallbackStarter(connector_of(client), "calls").abandon(new_id())
    assert terminated is (error is None)
    assert not any(e["event"] == "media.callback_abandon_failed" for e in log_events(stream))


@pytest.mark.parametrize(
    ("terminate_error", "connect_error"),
    [(RPCError("down", RPCStatusCode.UNAVAILABLE, b""), None), (None, OSError("refused"))],
)
async def test_abandon_never_raises(terminate_error: RPCError | None, connect_error: OSError | None) -> None:
    stream = json_log_stream()
    client = SignalRecorder()
    client.terminate_error = terminate_error
    assert not await TemporalCallbackStarter(connector_of(client, connect_error), "calls").abandon(new_id())
    [failed] = [e for e in log_events(stream) if e["event"] == "media.callback_abandon_failed"]
    assert failed["error_type"] in {"RPCError", "OSError"}
