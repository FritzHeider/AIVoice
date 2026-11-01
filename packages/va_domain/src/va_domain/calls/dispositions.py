"""Disposition tags every call knows besides the configured ones (specs/05-calls-api.md §3.6)."""

from __future__ import annotations

from typing import Final

__all__ = ["BUILTIN_DISPOSITIONS", "SYSTEM_DISPOSITIONS"]

# Always offered to the post-call LLM, appended after the configured list when absent from it.
BUILTIN_DISPOSITIONS: Final = (
    "INTERESTED",
    "NOT_INTERESTED",
    "FOLLOW_UP_REQUIRED",
    "CALL_BACK_SCHEDULED",
    "TRANSFERRED",
    "DO_NOT_CONTACT",
)
# Assigned by the platform from the call outcome; never chosen by the LLM.
SYSTEM_DISPOSITIONS: Final = ("NO_ANSWER", "BUSY", "CANCELED", "FAILED", "AGENT_ENDED_CALL", "VOICEMAIL")
