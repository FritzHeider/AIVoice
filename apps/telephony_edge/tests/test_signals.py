"""``TemporalCarrierSignaller`` (specs/06-telephony-telnyx.md §5 Decision P05-T02 item 10)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from structlog.testing import capture_logs
from temporalio.client import Client
from temporalio.converter import DataConverter
from temporalio.service import RPCError, RPCStatusCode

from va_common.checks import TemporalConnector
from va_domain.carrier import CarrierEvent
from va_edge.signals import TemporalCarrierSignaller

EVENT = CarrierEvent(
    type="call.answered",
    leg="primary",
    call_id=uuid.UUID("0190f5c2-0000-7000-8000-00000000000a"),
    org_id=uuid.UUID(int=3),
    telnyx_event_id="evt-1",
    occurred_at=datetime(2026, 11, 1, 12, tzinfo=UTC),
    payload={"call_control_id": "v3:x"},
)


@dataclass
class FakeHandle:
    error: Exception | None
    signals: list[tuple[str, Any]] = field(default_factory=list)

    async def signal(self, name: str, arg: Any) -> None:
        self.signals.append((name, arg))
        if self.error is not None:
            raise self.error


@dataclass
class FakeClient:
    error: Exception | None = None
    handles: dict[str, FakeHandle] = field(default_factory=dict)

    def get_workflow_handle(self, workflow_id: str) -> FakeHandle:
        return self.handles.setdefault(workflow_id, FakeHandle(self.error))


def signaller(client: FakeClient) -> TemporalCarrierSignaller:
    async def connect(_host: str, _namespace: str, _converter: DataConverter | None) -> Client:
        return cast("Client", client)

    return TemporalCarrierSignaller(TemporalConnector("temporal.test:7233", "default", connect=connect))


async def test_signals_the_call_workflow_with_the_json_event() -> None:
    client = FakeClient()
    await signaller(client).signal(EVENT)
    handle = client.handles["call-0190f5c2-0000-7000-8000-00000000000a"]
    assert handle.signals == [("signal_carrier_event", EVENT.model_dump(mode="json"))]
    assert CarrierEvent.model_validate(handle.signals[0][1]) == EVENT


async def test_missing_workflow_is_logged_and_tolerated() -> None:
    client = FakeClient(RPCError("workflow not found", RPCStatusCode.NOT_FOUND, b""))
    with capture_logs() as logs:
        await signaller(client).signal(EVENT)
    assert [entry["event"] for entry in logs] == ["telnyx.workflow_missing"]
    assert logs[0]["telnyx_event_id"] == "evt-1"


async def test_other_rpc_errors_propagate() -> None:
    client = FakeClient(RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b""))
    with pytest.raises(RPCError):
        await signaller(client).signal(EVENT)
