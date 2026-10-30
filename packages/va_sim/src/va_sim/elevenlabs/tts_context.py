"""One TTS context of a multi-context socket (specs/30 §3.3, specs/09 §3; Decision P03-T04 rules 5-7).

A context buffers text and decides when a generation starts: on ``flush`` or when the stripped buffer reaches
the next ``chunk_length_schedule`` threshold (the k-th generation uses element ``min(k, len - 1)``). A
generation takes the whole buffer verbatim; a whitespace-only buffer never generates (a flush discards it).
"""

from __future__ import annotations

from typing import Final

from va_sim.elevenlabs.store import TtsRequest

__all__ = ["DEFAULT_SCHEDULE", "MAX_CONTEXTS", "TtsContext", "parse_schedule"]

DEFAULT_SCHEDULE: Final = (120, 160, 250, 290)
MAX_CONTEXTS: Final = 5
_SCHEDULE_MIN: Final = 50
_SCHEDULE_MAX: Final = 500
_SCHEDULE_MAX_LEN: Final = 4


def parse_schedule(generation_config: object) -> tuple[int, ...]:
    """The ``chunk_length_schedule`` of a context's first message; ``ValueError`` when invalid."""
    if generation_config is None:
        return DEFAULT_SCHEDULE
    if not isinstance(generation_config, dict):
        msg = "generation_config must be an object"
        raise ValueError(msg)  # noqa: TRY004 - reported as an invalid_message frame, not a type error
    schedule = generation_config.get("chunk_length_schedule")
    if schedule is None:
        return DEFAULT_SCHEDULE
    if not isinstance(schedule, list) or not 1 <= len(schedule) <= _SCHEDULE_MAX_LEN:
        msg = f"chunk_length_schedule must be a list of 1 to {_SCHEDULE_MAX_LEN} integers"
        raise ValueError(msg)
    values: list[int] = []
    for value in schedule:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not _SCHEDULE_MIN <= value <= _SCHEDULE_MAX
        ):
            msg = f"chunk_length_schedule values must be integers from {_SCHEDULE_MIN} to {_SCHEDULE_MAX}"
            raise ValueError(msg)
        values.append(value)
    return tuple(values)


class TtsContext:
    """Text buffer, generation trigger, and the request record of one context."""

    def __init__(self, context_id: str, schedule: tuple[int, ...], record: TtsRequest) -> None:
        self.id = context_id
        self.schedule = schedule
        self.record = record
        self.buffer = ""
        self.generations = 0

    def add_text(self, text: str) -> None:
        self.record.text += text
        self.buffer += text

    def threshold(self) -> int:
        """Stripped buffer length that starts the next generation without a flush."""
        return self.schedule[min(self.generations, len(self.schedule) - 1)]

    def take(self, *, flush: bool) -> str | None:
        """The text of a generation that starts now, or ``None`` (the buffer keeps waiting)."""
        if not self.buffer.strip():
            if flush:
                self.buffer = ""
            return None
        if not flush and len(self.buffer.strip()) < self.threshold():
            return None
        text, self.buffer = self.buffer, ""
        self.generations += 1
        self.record.generated_text += text
        self.record.characters += len(text)
        return text

    def discard(self) -> bool:
        """Drop the unflushed buffer; ``True`` when it held anything but whitespace."""
        dropped = bool(self.buffer.strip())
        self.buffer = ""
        return dropped
