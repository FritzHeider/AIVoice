"""The no-input timer of one leg (specs/07-media-pipeline.md §12; Decision P04-T12c).

:class:`NoInputTimer` is pure and synchronous. The pipeline arms it when the leg goes idle (the agent's
utterance finished, nobody is speaking, nothing is queued), disarms it on any caller input, and asks it what
to do when the deadline passes: ``"reprompt"`` while fewer than ``max_reprompts`` reprompts were spoken since
the last user turn, else ``"goodbye"``.

A Brain that owns its own wait (a pathway Wait for Response node, Decision P07-T12a) arms the timer with
``timeout_ms``: such an expiry is ``"brain"``. It counts no reprompt and never says goodbye; the Brain's
``on_no_input`` decides what happens.
"""

from __future__ import annotations

from typing import Literal

__all__ = ["NoInputAction", "NoInputTimer"]

type NoInputAction = Literal["reprompt", "goodbye", "brain"]


class NoInputTimer:
    """Deadline and reprompt count of the no-input policy."""

    __slots__ = ("_brain_owned", "_deadline_ms", "_max_reprompts", "_reprompts", "_timeout_ms")

    def __init__(self, timeout_ms: float, max_reprompts: int) -> None:
        if timeout_ms <= 0:
            message = "timeout_ms must be > 0"
            raise ValueError(message)
        if max_reprompts < 0:
            message = "max_reprompts must be >= 0"
            raise ValueError(message)
        self._timeout_ms = timeout_ms
        self._max_reprompts = max_reprompts
        self._reprompts = 0
        self._deadline_ms: float | None = None
        self._brain_owned = False

    @property
    def deadline_ms(self) -> float | None:
        """When the timer expires (``None`` while disarmed)."""
        return self._deadline_ms

    @property
    def reprompts(self) -> int:
        """Reprompts spoken since the last user turn."""
        return self._reprompts

    @property
    def brain_owned(self) -> bool:
        """The armed deadline is the Brain's own wait (``timeout_ms`` given to :meth:`arm`)."""
        return self._deadline_ms is not None and self._brain_owned

    def arm(self, now_ms: float, *, timeout_ms: float | None = None) -> bool:
        """Start the timer at ``now_ms`` unless it already runs (an armed deadline is never extended).

        ``timeout_ms`` replaces the configured timeout and makes the expiry the Brain's (``"brain"``).
        """
        if self._deadline_ms is not None:
            return False
        if timeout_ms is not None and timeout_ms <= 0:
            message = "timeout_ms must be > 0"
            raise ValueError(message)
        self._brain_owned = timeout_ms is not None
        self._deadline_ms = now_ms + (self._timeout_ms if timeout_ms is None else timeout_ms)
        return True

    def disarm(self) -> bool:
        """Stop the timer; ``True`` when it was running."""
        armed = self._deadline_ms is not None
        self._deadline_ms = None
        return armed

    def due(self, now_ms: float) -> bool:
        """The armed deadline has passed."""
        return self._deadline_ms is not None and now_ms >= self._deadline_ms

    def expire(self) -> NoInputAction:
        """The deadline passed with no input: disarm and count a reprompt, or give up (a Brain-owned wait:
        ``"brain"``, nothing counted)."""
        self._deadline_ms = None
        if self._brain_owned:
            self._brain_owned = False
            return "brain"
        if self._reprompts < self._max_reprompts:
            self._reprompts += 1
            return "reprompt"
        return "goodbye"

    def reset(self) -> None:
        """A user turn reached the Brain: the reprompt count starts over (§12)."""
        self._reprompts = 0
