"""Carrier events to ``CallWorkflow`` (specs/06-telephony-telnyx.md §5 Decision P05-T02 item 10).

The edge signals ``signal_carrier_event`` on workflow ``call-<id>`` with the JSON form of the
:class:`va_domain.carrier.CarrierEvent` (``model_dump(mode="json")``, so the default Temporal data converter
carries it; the workflow validates it back). A workflow that does not exist (not yet
started, or already finished) is not an error: the event was stored, the carrier state hash and the carrier
channel still carry it, and a finished workflow has no use for it. Any other failure propagates, so the event
is retried by the reprocessor (the workflow drops redeliveries by ``telnyx_event_id``).
"""

from __future__ import annotations

from typing import Protocol

from temporalio.service import RPCError, RPCStatusCode

from va_common.checks import TemporalConnector
from va_common.logging import get_logger
from va_domain.carrier import CARRIER_SIGNAL, CarrierEvent, call_workflow_id

__all__ = ["CarrierSignaller", "TemporalCarrierSignaller"]

_log = get_logger(__name__)


class CarrierSignaller(Protocol):
    async def signal(self, event: CarrierEvent) -> None: ...


class TemporalCarrierSignaller:
    """Signals ``CallWorkflow.signal_carrier_event`` through the lazily connected Temporal client."""

    def __init__(self, connector: TemporalConnector) -> None:
        self._connector = connector

    async def signal(self, event: CarrierEvent) -> None:
        client = await self._connector.client()
        handle = client.get_workflow_handle(call_workflow_id(event.call_id))
        try:
            await handle.signal(CARRIER_SIGNAL, event.model_dump(mode="json"))
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
            _log.debug(
                "telnyx.workflow_missing",
                event_type=event.type,
                telnyx_event_id=event.telnyx_event_id,
                call_id=str(event.call_id),
            )
