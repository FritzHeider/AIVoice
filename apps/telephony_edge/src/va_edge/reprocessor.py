"""``TelnyxEventReprocessor`` (specs/06-telephony-telnyx.md §4.1 step 7, Decision P05-T01).

Every 5 s it claims, through ``TelnyxEventRepo.claim_retries``, the events that were never processed for
30 s (refused by a full shard queue, lost by a crashed process, or whose processed mark failed) and the failed
events with fewer than 5 attempts, and offers them to the :class:`va_edge.processor.EventProcessor`. Telnyx
therefore never has to retry a webhook the edge stored.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from va_common.logging import get_logger
from va_common.time import Clock
from va_db.repositories import TelnyxEventRepo
from va_db.session import Database
from va_edge.processor import MAX_ATTEMPTS, EventProcessor, StoredEvent

__all__ = ["INTERVAL_S", "RETRY_AFTER_S", "STALE_AFTER_S", "TelnyxEventReprocessor"]

INTERVAL_S: Final = 5.0
STALE_AFTER_S: Final = 30.0
RETRY_AFTER_S: Final = 5.0

_log = get_logger(__name__)


class TelnyxEventReprocessor:
    """Periodically re-offers stored events that still need processing."""

    def __init__(  # noqa: PLR0913 - keyword-only policy knobs
        self,
        database: Database,
        processor: EventProcessor,
        *,
        clock: Clock,
        interval_s: float = INTERVAL_S,
        stale_after_s: float = STALE_AFTER_S,
        retry_after_s: float = RETRY_AFTER_S,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self._database = database
        self._processor = processor
        self._clock = clock
        self._interval_s = interval_s
        self._stale_after = timedelta(seconds=stale_after_s)
        self._retry_after = timedelta(seconds=retry_after_s)
        self._max_attempts = max_attempts

    async def run_once(self) -> int:
        """Claim due events and offer them; returns how many were queued."""
        async with self._database.system_session() as session:
            rows = await TelnyxEventRepo(session).claim_retries(
                now=self._clock.now(),
                stale_after=self._stale_after,
                retry_after=self._retry_after,
                max_attempts=self._max_attempts,
            )
            events = [StoredEvent.from_row(row) for row in rows]
        queued = sum(self._processor.offer(event) for event in events)
        if events:
            _log.info("telnyx.events_reclaimed", claimed=len(events), queued=queued)
        return queued

    async def run(self) -> None:
        """Tick every ``interval_s`` until cancelled; a failed tick is logged and retried at the next one."""
        while True:
            await self._clock.sleep(self._interval_s)
            try:
                await self.run_once()
            except Exception as exc:  # the loop must survive database outages; the rows stay due
                _log.error("telnyx.reprocess_failed", error_type=type(exc).__name__)
