"""Keypad input of one leg (specs/07-media-pipeline.md §13; Decision P04-T12c).

:class:`DtmfBuffer` is pure and synchronous: digits collect into a sequence that ends after
:data:`DTMF_SEQUENCE_TIMEOUT_MS` without a new digit, or at once on :data:`DTMF_TERMINATOR` (``#``, which is
not part of the sequence). A :class:`DtmfSequence` becomes the user turn ``"[DTMF: 1 2 3 4]"`` with
``dtmf="1234"``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = [
    "DTMF_DIGITS",
    "DTMF_SEQUENCE_TIMEOUT_MS",
    "DTMF_TERMINATOR",
    "DtmfBuffer",
    "DtmfSequence",
    "is_dtmf_digit",
]

DTMF_SEQUENCE_TIMEOUT_MS: Final = 2000
"""A sequence ends this long after its last digit (§13)."""

DTMF_TERMINATOR: Final = "#"
"""Ends a sequence at once; never part of it."""

DTMF_DIGITS: Final = frozenset("0123456789*#ABCD")
"""The 16 DTMF symbols."""


def is_dtmf_digit(digit: str) -> bool:
    """``digit`` is exactly one DTMF symbol (``0``-``9``, ``*``, ``#``, ``A``-``D``)."""
    return digit in DTMF_DIGITS


@dataclass(frozen=True, slots=True)
class DtmfSequence:
    """A finished keypad sequence on the pipeline timeline (ms)."""

    digits: str
    first_ms: float
    last_ms: float

    @property
    def text(self) -> str:
        """The user-turn text: ``"[DTMF: 1 2 3 4]"``."""
        return f"[DTMF: {' '.join(self.digits)}]"


class DtmfBuffer:
    """Collects digits into sequences; the caller sleeps until :attr:`deadline_ms` and then :meth:`poll` s."""

    __slots__ = ("_digits", "_first_ms", "_last_ms", "_timeout_ms")

    def __init__(self, timeout_ms: float = DTMF_SEQUENCE_TIMEOUT_MS) -> None:
        if timeout_ms <= 0:
            message = "timeout_ms must be > 0"
            raise ValueError(message)
        self._timeout_ms = timeout_ms
        self._digits: list[str] = []
        self._first_ms = 0.0
        self._last_ms = 0.0

    @property
    def pending(self) -> bool:
        """Digits wait for the end of their sequence."""
        return bool(self._digits)

    @property
    def deadline_ms(self) -> float | None:
        """When the pending sequence ends without another digit (``None`` when nothing is pending)."""
        return self._last_ms + self._timeout_ms if self._digits else None

    def press(self, digit: str, now_ms: float) -> DtmfSequence | None:
        """Add ``digit``; the terminator returns the finished sequence (``None`` when nothing was pending).

        Raises ``ValueError`` for anything but one DTMF symbol.
        """
        if not is_dtmf_digit(digit):
            message = f"not a DTMF digit: {digit!r}"
            raise ValueError(message)
        if digit == DTMF_TERMINATOR:
            return self._take()
        if not self._digits:
            self._first_ms = now_ms
        self._digits.append(digit)
        self._last_ms = now_ms
        return None

    def poll(self, now_ms: float) -> DtmfSequence | None:
        """The pending sequence once its deadline has passed, else ``None``."""
        deadline = self.deadline_ms
        if deadline is None or now_ms < deadline:
            return None
        return self._take()

    def clear(self) -> int:
        """Drop the pending digits; returns how many were dropped."""
        dropped = len(self._digits)
        self._digits = []
        return dropped

    def _take(self) -> DtmfSequence | None:
        if not self._digits:
            return None
        sequence = DtmfSequence("".join(self._digits), self._first_ms, self._last_ms)
        self._digits = []
        return sequence
