"""The IVR dead-end timer of one leg (specs/05-calls-api.md §9 ``ivr_dead_end``;
specs/07-media-pipeline.md §11 Decision P05-T10b).

:class:`IvrProgress` is pure and synchronous. With ``ivr_mode`` the pipeline starts it when the opener starts
and ends the call with ``ivr_dead_end`` once ``dead_end_ms`` pass without progress. Progress is a delivered
user turn whose normalized text (``va_voice.text_norm.normalized``) was not delivered before in this call — a
menu that repeats itself is no progress — or any telephony action the Brain took (``press_button``).
"""

from __future__ import annotations

from va_voice.text_norm import normalized

__all__ = ["IvrProgress"]


class IvrProgress:
    """Deadline of the IVR dead end and the texts already heard."""

    __slots__ = ("_dead_end_ms", "_deadline_ms", "_heard")

    def __init__(self, dead_end_ms: float) -> None:
        if dead_end_ms <= 0:
            message = "dead_end_ms must be > 0"
            raise ValueError(message)
        self._dead_end_ms = dead_end_ms
        self._deadline_ms: float | None = None
        self._heard: set[str] = set()

    @property
    def deadline_ms(self) -> float | None:
        """When the call is a dead end (``None`` before :meth:`start`)."""
        return self._deadline_ms

    def start(self, now_ms: float) -> None:
        """The opener started: the first dead-end window opens."""
        self._deadline_ms = now_ms + self._dead_end_ms

    def on_turn(self, text: str, now_ms: float) -> bool:
        """A user turn was delivered; ``True`` (and the window restarts) when its text is new."""
        key = normalized(text)
        if not key or key in self._heard:
            return False
        self._heard.add(key)
        self._restart(now_ms)
        return True

    def on_action(self, now_ms: float) -> None:
        """The Brain took a telephony action: progress."""
        self._restart(now_ms)

    def due(self, now_ms: float) -> bool:
        """The window passed without progress."""
        return self._deadline_ms is not None and now_ms >= self._deadline_ms

    def _restart(self, now_ms: float) -> None:
        if self._deadline_ms is not None:
            self._deadline_ms = now_ms + self._dead_end_ms
