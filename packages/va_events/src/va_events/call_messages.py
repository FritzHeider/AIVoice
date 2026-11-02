"""Messages of the call creation events (specs/19-events-and-webhooks.md §2), shared by the Send Call
dispatch and the voicemail retry activity (specs/07 §14 Decision P05-T10f)."""

from __future__ import annotations

from typing import Final

__all__ = ["QUEUED_MESSAGE", "SCHEDULED_MESSAGE"]

QUEUED_MESSAGE: Final = "Call successfully queued."
SCHEDULED_MESSAGE: Final = "Call successfully scheduled."
