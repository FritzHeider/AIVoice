"""Carrier events to ``CallWorkflow`` (specs/06-telephony-telnyx.md §5 Decision P05-T02 item 10).

The edge signals ``signal_carrier_event`` on workflow ``call-<id>`` with the JSON form of the
:class:`va_domain.carrier.CarrierEvent` (``model_dump(mode="json")``, so the default Temporal data converter
carries it; the workflow validates it back). A workflow that does not exist (not yet
started, or already finished) is not an error: the event was stored, the carrier state hash and the carrier
channel still carry it, and a finished workflow has no use for it. Any other failure propagates, so the event
is retried by the reprocessor (the workflow drops redeliveries by ``telnyx_event_id``).

``call.recording.saved`` also goes to ``PostCallWorkflow`` (``postcall-<id>``, signal ``recording_saved``,
same JSON; specs/06 §11 Decision P05-T14). The post-call workflow usually starts after the recording is
saved, so a missing one is expected: it reads the stored event when it starts.
"""

from __future__ import annotations

from typing import Final, Protocol

from va_common.checks import TemporalConnector, signal_workflow
from va_common.logging import get_logger
from va_domain.carrier import CARRIER_SIGNAL, CarrierEvent, call_workflow_id
from va_domain.post_call import RECORDING_SAVED_SIGNAL, postcall_workflow_id

__all__ = ["CarrierSignaller", "TemporalCarrierSignaller"]

_log = get_logger(__name__)

RECORDING_SAVED: Final = "call.recording.saved"


class CarrierSignaller(Protocol):
    async def signal(self, event: CarrierEvent) -> None: ...


class TemporalCarrierSignaller:
    """Signals ``CallWorkflow.signal_carrier_event`` through the lazily connected Temporal client."""

    def __init__(self, connector: TemporalConnector) -> None:
        self._connector = connector

    async def signal(self, event: CarrierEvent) -> None:
        body = event.model_dump(mode="json")
        if not await signal_workflow(self._connector, call_workflow_id(event.call_id), CARRIER_SIGNAL, body):
            _log.debug(
                "telnyx.workflow_missing",
                event_type=event.type,
                telnyx_event_id=event.telnyx_event_id,
                call_id=str(event.call_id),
            )
        if event.type != RECORDING_SAVED:
            return
        workflow_id = postcall_workflow_id(event.call_id)
        if not await signal_workflow(self._connector, workflow_id, RECORDING_SAVED_SIGNAL, body):
            _log.debug(
                "telnyx.postcall_missing", telnyx_event_id=event.telnyx_event_id, call_id=str(event.call_id)
            )
