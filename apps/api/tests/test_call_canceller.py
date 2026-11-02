"""``TemporalCallCanceller`` (specs/05 §7.3 Decision P05-T11c): ``signal_cancel`` without an argument."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from temporalio.service import RPCError, RPCStatusCode
from workflow_fakes import NO_ARG, SignalRecorder, connector_of

from va_api.services.call_stop import CallCancelError, TemporalCallCanceller

CALL_ID = uuid.UUID("0190f5c2-0000-7000-8000-0000000000c1")


async def test_signals_cancel_without_an_argument() -> None:
    client = SignalRecorder()
    assert await TemporalCallCanceller(connector_of(client)).cancel(CALL_ID)
    assert client.sent == [(f"call-{CALL_ID}", "signal_cancel", ())]


async def test_a_missing_workflow_is_false() -> None:
    client = SignalRecorder(RPCError("not found", RPCStatusCode.NOT_FOUND, b""))
    assert not await TemporalCallCanceller(connector_of(client)).cancel(CALL_ID)


@pytest.mark.parametrize(
    "error",
    [RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b""), OSError("refused"), RuntimeError("closed")],
)
async def test_signal_failures_are_cancel_errors(error: Exception) -> None:
    canceller = TemporalCallCanceller(connector_of(SignalRecorder(error)))
    with pytest.raises(CallCancelError) as caught:
        await canceller.cancel(CALL_ID)
    assert caught.value.__cause__ is error


async def test_a_failed_connect_is_a_cancel_error() -> None:
    canceller = TemporalCallCanceller(connector_of(SignalRecorder(), RuntimeError("connect refused")))
    with pytest.raises(CallCancelError):
        await canceller.cancel(CALL_ID)


class _Hanging(SignalRecorder):
    async def signal(self, name: str, arg: Any = NO_ARG, *, args: Sequence[Any] = ()) -> None:
        await super().signal(name, arg, args=args)
        await asyncio.Event().wait()


async def test_a_slow_signal_times_out() -> None:
    canceller = TemporalCallCanceller(connector_of(_Hanging()), timeout_s=0.01)
    with pytest.raises(CallCancelError) as caught:
        await canceller.cancel(CALL_ID)
    assert isinstance(caught.value.__cause__, TimeoutError)
