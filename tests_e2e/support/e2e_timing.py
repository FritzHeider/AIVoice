"""Pure timing helpers of the barge-in and interruptibility flows (Decision P05-T17d under specs/30 §5).

They read the FakeTelnyx timeline (``utterances`` with ``start_ms``/``end_ms``, ``clears``: the sim
``virtual_ms`` of every agent ``clear`` frame), the call event log, and FakeLLM request messages. No stack
access.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from va_events import DEFAULT_CATEGORY, EventType
from va_voice.vad import WINDOW_MS

__all__ = [
    "INTERRUPT_CATEGORY",
    "ONSET_TOLERANCE_MS",
    "Window",
    "assistant_before",
    "clears_in",
    "decision_speech_ms",
    "first_clear_after",
    "interrupt_events",
    "utterance_window",
    "whole_word_prefix",
]

INTERRUPT_CATEGORY: Final = DEFAULT_CATEGORY[EventType.CALL_INTERRUPT]
# Silero stamps ``speech_start`` at the start of the first 32 ms window that holds voice. The window grid is
# the stream's, not the utterance's, so that window may begin up to one window before the caller's voice onset
# (Decision P08-T00 under specs/30 §5): a sim-side "clear after onset" check allows this much.
ONSET_TOLERANCE_MS: Final = int(WINDOW_MS)


@dataclass(frozen=True, slots=True)
class Window:
    """A half-open sim time range ``[start_ms, end_ms)``."""

    start_ms: int
    end_ms: int

    def __contains__(self, at_ms: object) -> bool:
        return isinstance(at_ms, int) and self.start_ms <= at_ms < self.end_ms


def utterance_window(entry: Mapping[str, Any], tail_ms: int) -> Window:
    """From a timeline utterance's ``start_ms`` to ``tail_ms`` after its ``end_ms`` (it must have ended)."""
    end_ms = entry.get("end_ms")
    if end_ms is None:
        message = f"the utterance has not ended: {entry!r}"
        raise AssertionError(message)
    return Window(int(entry["start_ms"]), int(end_ms) + tail_ms)


def clears_in(timeline: Mapping[str, Any], window: Window) -> list[int]:
    """The ``clear`` times of ``timeline`` inside ``window``."""
    return [at for at in timeline["clears"] if at in window]


def first_clear_after(timeline: Mapping[str, Any], at_ms: int) -> int | None:
    """The first ``clear`` at or after ``at_ms`` (``None`` when there is none)."""
    return next((at for at in timeline["clears"] if at >= at_ms), None)


def interrupt_events(events: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The ``call.interrupt`` rows of an event log (the only event type of their category)."""
    return [event for event in events if event["category"] == INTERRUPT_CATEGORY]


def decision_speech_ms(event: Mapping[str, Any]) -> int:
    """The caller speech (ms, pipeline timeline) the interruption policy counted when it decided to clear,
    from a ``call.interrupt`` row (``data.speech_ms``)."""
    value = event["data"].get("speech_ms")
    assert isinstance(value, int), f"call.interrupt without an integer speech_ms: {event!r}"
    return value


def assistant_before(messages: Sequence[Mapping[str, Any]], user_text: str) -> str | None:
    """The assistant message right before the first user message containing ``user_text`` (``None`` when
    there is no such user message or the message before it is not the assistant's)."""
    for index, message in enumerate(messages):
        content = message.get("content")
        if message.get("role") == "user" and isinstance(content, str) and user_text in content:
            if index == 0:
                return None
            previous = messages[index - 1]
            spoken = previous.get("content")
            if previous.get("role") != "assistant" or not isinstance(spoken, str):
                return None
            return spoken
    return None


def whole_word_prefix(text: str, chars: float) -> int:
    """The length of the longest prefix of ``text`` that ends at a word end within its first ``chars``
    characters (``text_spoken`` keeps whole words only, specs/07 §9)."""
    limit = min(int(chars), len(text))
    if limit == len(text):
        return len(text)
    for end in range(limit, 0, -1):
        if text[end].isspace() and not text[end - 1].isspace():
            return end
    return 0
