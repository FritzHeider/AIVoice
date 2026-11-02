"""The takeover seam of the pipeline: something outside the conversation ends the call
(specs/07-media-pipeline.md §14; specs/07 §14 Decision P05-T10c).

:meth:`va_voice.pipeline.CallPipeline.take_over` accepts one :class:`Takeover` (the voicemail monitor's
``voicemail_hangup`` / ``voicemail_left`` / ``fax_detected``). The pipeline stops accepting turns, cuts the
reply in progress off, and, when the takeover carries ``text``, waits for the message cue before speaking it
once (not interruptible): the ``beep`` event, or ``silence_ms`` without caller speech (:class:`SpeechCue`),
at most
``max_wait_s``. Then it ends with ``reason``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Final

from va_voice.vad import VadEvent

__all__ = [
    "DEFAULT_MESSAGE_SILENCE_MS",
    "DEFAULT_MESSAGE_WAIT_S",
    "DEFAULT_SPEAK_TIMEOUT_S",
    "SpeechCue",
    "Takeover",
]

DEFAULT_MESSAGE_SILENCE_MS: Final = 1200
"""Silence after the greeting that cues the message when no beep is reported (§14 step 4)."""

DEFAULT_MESSAGE_WAIT_S: Final = 20.0
"""Longest wait for the beep or the silence before the message is spoken anyway (§14 step 4)."""

DEFAULT_SPEAK_TIMEOUT_S: Final = 120.0
"""Cap on the message playback (waiting for its mark echo)."""


@dataclass(frozen=True, slots=True)
class Takeover:
    """End the call with ``reason``; speak ``text`` (when not blank) once after the message cue first."""

    reason: str
    text: str | None = None
    beep: asyncio.Event | None = None
    silence_ms: int = DEFAULT_MESSAGE_SILENCE_MS
    max_wait_s: float = DEFAULT_MESSAGE_WAIT_S
    speak_timeout_s: float = DEFAULT_SPEAK_TIMEOUT_S


class SpeechCue:
    """The caller's VAD state for the silence cue: whether they speak now and when they last stopped."""

    def __init__(self) -> None:
        self.speaking = False
        self.last_speech_end_ms: float | None = None

    def on_vad(self, event: VadEvent) -> None:
        if event.kind == "speech_start":
            self.speaking = True
        elif event.kind == "speech_end":
            self.speaking = False
            self.last_speech_end_ms = event.at_ms

    def silence_at_ms(self, started_ms: float, silence_ms: float) -> float | None:
        """When ``silence_ms`` of silence will have passed (``None`` while the caller speaks): counted from
        the last speech end, or from ``started_ms`` when nobody spoke yet."""
        if self.speaking:
            return None
        since = self.last_speech_end_ms if self.last_speech_end_ms is not None else started_ms
        return since + silence_ms
