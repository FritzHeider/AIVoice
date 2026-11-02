"""Carrier facts of an attached call for the pipeline (specs/07-media-pipeline.md §11 steps 1-2, §14;
Decisions P05-T10b, P05-T10c and specs/06 §10.1 Decision P05-T13).

:class:`CarrierWatch` combines the carrier state snapshot read at attach with the carrier events published to
the call's channel afterwards (only events of the ``primary`` leg count):

- :attr:`CarrierWatch.answered` is set when the snapshot says ``answered`` or a ``call.answered`` event
  arrives;
- :attr:`CarrierWatch.amd` is set with the first AMD verdict (:attr:`CarrierWatch.verdict`): the snapshot's
  ``answered_by`` (premium) or ``amd_result``, or the first ``call.machine.detection.ended`` /
  ``call.machine.premium.detection.ended`` event; :attr:`CarrierWatch.on_verdict` is called once with it;
- :attr:`CarrierWatch.beep` is set by the snapshot's ``beep_detected`` / ``greeting_ended`` fields or a
  ``call.machine.premium.greeting.ended`` / ``call.machine.greeting.ended`` event (the voicemail message cue);
- ``call.machine.premium.call_screening.detected`` is logged and treated as a human (the opener proceeds);
- events of the ``transfer_target`` leg go to :attr:`CarrierWatch.transfer_events`, where a running
  :class:`~va_media.transfer.ColdTransfer` waits for the bridge or the target's hangup (Decision P05-T13).

:meth:`CarrierWatch.run` is the only reader of the feed's carrier queue (run it as a task next to the
pipeline); other event types are counted and left for later handlers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Final

from va_common.logging import get_logger
from va_domain.carrier import CarrierEvent
from va_domain.voicemail import AmdVerdict, amd_verdict, verdict_of_answered_by
from va_media.carrier_state import CarrierSnapshot
from va_media.channels import CallFeed

__all__ = ["ANSWERED_EVENT", "BEEP_EVENTS", "SCREENING_EVENT", "CarrierWatch", "snapshot_verdict"]

ANSWERED_EVENT: Final = "call.answered"
BEEP_EVENTS: Final = frozenset({"call.machine.premium.greeting.ended", "call.machine.greeting.ended"})
SCREENING_EVENT: Final = "call.machine.premium.call_screening.detected"
_PREMIUM_DETECTION: Final = "call.machine.premium.detection.ended"

_log = get_logger(__name__)


def snapshot_verdict(snapshot: CarrierSnapshot) -> AmdVerdict | None:
    """The verdict the edge stored before attach: ``answered_by`` (premium) first, else ``amd_result`` (the
    edge stores a premium result raw and a standard one normalized, both premium spellings)."""
    if snapshot.answered_by is not None:
        return verdict_of_answered_by(snapshot.answered_by)
    if snapshot.amd_result is not None:
        return amd_verdict(_PREMIUM_DETECTION, snapshot.amd_result)
    return None


class CarrierWatch:
    """The carrier facts of one call as events the media runtime can wait on."""

    def __init__(self, feed: CallFeed, snapshot: CarrierSnapshot) -> None:
        self._feed = feed
        self.answered = asyncio.Event()
        self.amd = asyncio.Event()
        self.beep = asyncio.Event()
        self.verdict: AmdVerdict | None = None
        self.on_verdict: Callable[[AmdVerdict], None] | None = None
        self.events_seen = 0
        self.transfer_events: asyncio.Queue[CarrierEvent] = asyncio.Queue()
        if snapshot.answered:
            self.answered.set()
        fields = snapshot.fields
        if fields.get("beep_detected") == "1" or fields.get("greeting_ended") == "1":
            self.beep.set()
        verdict = snapshot_verdict(snapshot)
        if verdict is not None:
            self.verdict = verdict
            self.amd.set()

    async def run(self) -> None:
        """Read carrier events until cancelled."""
        while True:
            event = await self._feed.next_carrier()
            self.events_seen += 1
            if event.leg == "primary":
                self._on_primary(event)
            elif event.leg == "transfer_target":
                self.transfer_events.put_nowait(event)

    def _on_primary(self, event: CarrierEvent) -> None:
        if event.type == ANSWERED_EVENT:
            if not self.answered.is_set():
                _log.info("media.carrier_answered")
                self.answered.set()
        elif event.type in BEEP_EVENTS:
            if not self.beep.is_set():
                _log.info("media.voicemail_beep", event_type=event.type)
                self.beep.set()
        elif event.type == SCREENING_EVENT:
            _log.info("media.call_screening")
        else:
            result = event.payload.get("result")
            verdict = amd_verdict(event.type, result if isinstance(result, str) else None)
            if verdict is not None:
                self._set_verdict(verdict)

    def _set_verdict(self, verdict: AmdVerdict) -> None:
        if self.amd.is_set():
            _log.info("media.amd_repeated", verdict=verdict)
            return
        self.verdict = verdict
        self.amd.set()
        _log.info("media.amd_verdict", verdict=verdict)
        if self.on_verdict is not None:
            self.on_verdict(verdict)
