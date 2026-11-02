"""Carrier facts of an attached call for the pipeline (specs/07-media-pipeline.md §11 step 1;
Decision P05-T10b).

:class:`CarrierWatch` combines the carrier state snapshot read at attach with the carrier events published to
the call's channel afterwards: :attr:`CarrierWatch.answered` is set when the snapshot says ``answered`` or a
``call.answered`` event of the ``primary`` leg arrives. :meth:`CarrierWatch.run` is the only reader of the
feed's carrier queue (run it as a task next to the pipeline); events of other types are counted and left for
later handlers (AMD in P05-T10c).
"""

from __future__ import annotations

import asyncio
from typing import Final

from va_common.logging import get_logger
from va_media.carrier_state import CarrierSnapshot
from va_media.channels import CallFeed

__all__ = ["ANSWERED_EVENT", "CarrierWatch"]

ANSWERED_EVENT: Final = "call.answered"

_log = get_logger(__name__)


class CarrierWatch:
    """The carrier facts of one call as events the media runtime can wait on."""

    def __init__(self, feed: CallFeed, snapshot: CarrierSnapshot) -> None:
        self._feed = feed
        self.answered = asyncio.Event()
        self.events_seen = 0
        if snapshot.answered:
            self.answered.set()

    async def run(self) -> None:
        """Read carrier events until cancelled."""
        while True:
            event = await self._feed.next_carrier()
            self.events_seen += 1
            if event.type == ANSWERED_EVENT and event.leg == "primary" and not self.answered.is_set():
                _log.info("media.carrier_answered")
                self.answered.set()
