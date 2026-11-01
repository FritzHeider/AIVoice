"""Adaptive end-of-turn detection for one call leg (specs/07-media-pipeline.md §6).

:class:`Endpointer` is pure and synchronous: every input carries an explicit timestamp in milliseconds on
one timeline chosen by the caller (VAD stream time, with partial-transcript arrival times mapped onto it),
and it never reads a clock. The caller feeds VAD edges (:meth:`Endpointer.on_vad`) and STT partials
(:meth:`Endpointer.on_partial`), re-reads :meth:`Endpointer.deadline_ms` after every input, and calls
:meth:`Endpointer.poll` at that instant. ``poll`` returns :class:`EndOfTurn` once the silence since the
last voiced window reaches the timeout and the turn has text, or :class:`TurnDiscarded` for a noise burst
that never produced text before the text-wait cap.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from va_voice.text_norm import ends_sentence, ends_with_digit, normalized, tokens
from va_voice.vad import VadEvent

__all__ = [
    "ADAPTIVE_MAX_MS",
    "ADAPTIVE_MIN_MS",
    "BASE_SILENCE_MS",
    "COMPLETE_MULTIPLIER",
    "CONTINUATION_MULTIPLIER",
    "CONTINUATION_WORDS",
    "IVR_FLOOR_MS",
    "MIN_PAUSE_SAMPLES",
    "NUMBER_MULTIPLIER",
    "NUMBER_NAME_HINTS",
    "PAUSE_HISTORY",
    "PAUSE_MAX_MS",
    "PAUSE_MIN_MS",
    "SHORT_ANSWERS",
    "THRESHOLD_MAX_MS",
    "THRESHOLD_MIN_MS",
    "TIMEOUT_MAX_MS",
    "TIMEOUT_MIN_MS",
    "EndOfTurn",
    "EndOfTurnReason",
    "Endpointer",
    "EndpointerSettings",
    "ResumptionSpeed",
    "TurnDiscarded",
    "collects_number",
    "completeness_multiplier",
    "percentile",
]

ResumptionSpeed = Literal[1, 2, 3]
EndOfTurnReason = Literal["silence", "late_transcript"]

BASE_SILENCE_MS: Final[dict[int, float]] = {1: 300.0, 2: 500.0, 3: 800.0}
PAUSE_MIN_MS: Final = 120.0
PAUSE_MAX_MS: Final = 1500.0
PAUSE_HISTORY: Final = 20
MIN_PAUSE_SAMPLES: Final = 4
ADAPTIVE_MIN_MS: Final = 250.0
ADAPTIVE_MAX_MS: Final = 1500.0
TIMEOUT_MIN_MS: Final = 250.0
TIMEOUT_MAX_MS: Final = 2000.0
IVR_FLOOR_MS: Final = 900.0
THRESHOLD_MIN_MS: Final = 50
THRESHOLD_MAX_MS: Final = 5000
COMPLETE_MULTIPLIER: Final = 0.7
CONTINUATION_MULTIPLIER: Final = 1.6
NUMBER_MULTIPLIER: Final = 1.8

SHORT_ANSWERS: Final = frozenset(
    {
        "yes",
        "no",
        "yeah",
        "yep",
        "nope",
        "sure",
        "okay",
        "ok",
        "correct",
        "right",
        "thanks",
        "thank you",
        "bye",
        "goodbye",
        "hello",
        "hi",
    }
)
CONTINUATION_WORDS: Final = frozenset(
    {
        "and",
        "but",
        "so",
        "or",
        "because",
        "um",
        "uh",
        "er",
        "like",
        "the",
        "a",
        "an",
        "to",
        "of",
        "with",
        "my",
        "is",
        "was",
        "if",
        "then",
        "that",
        "which",
    }
)
NUMBER_NAME_HINTS: Final = ("phone", "zip", "number", "code")
_PERCENT: Final = 100


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)


def percentile(samples: Sequence[float], q: float) -> float:
    """``q``-th percentile (0-100), linear interpolation between closest ranks (numpy's default method)."""
    if not samples:
        msg = "percentile of an empty sequence"
        raise ValueError(msg)
    if not 0 <= q <= _PERCENT:
        msg = f"q must be within 0..100, got {q}"
        raise ValueError(msg)
    ordered = sorted(samples)
    rank = q / 100 * (len(ordered) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def collects_number(variables: Iterable[tuple[str, str]]) -> bool:
    """True when a node extracts a number: a ``(name, type)`` with type ``integer`` or a number-like name."""
    for name, kind in variables:
        lowered = name.lower()
        if kind == "integer" or any(hint in lowered for hint in NUMBER_NAME_HINTS):
            return True
    return False


def completeness_multiplier(raw_partial: str, *, collecting_number: bool) -> float:
    """Timeout multiplier for the latest partial transcript (§6 rule 4; number rule first)."""
    if collecting_number and ends_with_digit(raw_partial):
        return NUMBER_MULTIPLIER
    if ends_sentence(raw_partial) or normalized(raw_partial) in SHORT_ANSWERS:
        return COMPLETE_MULTIPLIER
    words = tokens(raw_partial)
    if words and words[-1] in CONTINUATION_WORDS:
        return CONTINUATION_MULTIPLIER
    return 1.0


@dataclass(frozen=True, slots=True)
class EndpointerSettings:
    """Timing settings of the current node/call.

    ``interruption_threshold_ms`` (call setting, 50-5000) replaces the adaptive timeout when set;
    ``collecting_number`` comes from :func:`collects_number` over the node's extraction variables.
    """

    resumption_speed: ResumptionSpeed = 2
    interruption_threshold_ms: int | None = None
    ivr_mode: bool = False
    collecting_number: bool = False

    def __post_init__(self) -> None:
        if self.resumption_speed not in BASE_SILENCE_MS:
            msg = f"resumption_speed must be 1, 2 or 3, got {self.resumption_speed}"
            raise ValueError(msg)
        threshold = self.interruption_threshold_ms
        if threshold is not None and not THRESHOLD_MIN_MS <= threshold <= THRESHOLD_MAX_MS:
            bounds = f"{THRESHOLD_MIN_MS}..{THRESHOLD_MAX_MS}"
            msg = f"interruption_threshold_ms must be within {bounds}, got {threshold}"
            raise ValueError(msg)

    @property
    def base_ms(self) -> float:
        return BASE_SILENCE_MS[self.resumption_speed]


@dataclass(frozen=True, slots=True)
class EndOfTurn:
    """The user's turn ended at ``at_ms`` (the decision time)."""

    at_ms: float
    reason: EndOfTurnReason
    last_voiced_end_ms: float
    timeout_ms: float
    text: str

    @property
    def eot_ms(self) -> float:
        """``eot_ms`` latency metric (§20): decision time minus the end of the last voiced window."""
        return self.at_ms - self.last_voiced_end_ms


@dataclass(frozen=True, slots=True)
class TurnDiscarded:
    """A speech segment without any transcript text (noise burst), dropped silently at ``at_ms``."""

    at_ms: float
    started_ms: float
    last_voiced_end_ms: float


class Endpointer:
    """End-of-turn decisions for one leg; pause adaptation persists across nodes until :meth:`reset`."""

    __slots__ = (
        "_first_text_ms",
        "_last_voiced_end_ms",
        "_partial",
        "_pauses",
        "_settings",
        "_speaking",
        "_started_ms",
        "_turn_open",
    )

    def __init__(self, settings: EndpointerSettings | None = None) -> None:
        self._settings = settings if settings is not None else EndpointerSettings()
        self._pauses: deque[float] = deque(maxlen=PAUSE_HISTORY)
        self._turn_open = False
        self._speaking = False
        self._started_ms = 0.0
        self._last_voiced_end_ms = 0.0
        self._partial = ""
        self._first_text_ms: float | None = None

    @property
    def settings(self) -> EndpointerSettings:
        return self._settings

    @property
    def turn_open(self) -> bool:
        """True from the first ``speech_start`` of a user turn until it ends or is discarded."""
        return self._turn_open

    @property
    def speaking(self) -> bool:
        return self._speaking

    @property
    def pause_samples(self) -> tuple[float, ...]:
        """Recorded intra-utterance pauses, oldest first (at most ``PAUSE_HISTORY``)."""
        return tuple(self._pauses)

    def configure(self, settings: EndpointerSettings) -> None:
        """Apply new node timing settings; the caller's pause history is kept."""
        self._settings = settings

    def reset(self) -> None:
        """Forget everything, including the pause history (new leg)."""
        self._pauses.clear()
        self._close_turn()

    def on_vad(self, event: VadEvent) -> None:
        """Feed a VAD edge; ``probability`` events, a repeated start and an end while silent are ignored."""
        if event.kind == "speech_start" and not self._speaking:
            self._on_speech_start(event.at_ms)
        elif event.kind == "speech_end" and self._speaking:
            self._speaking = False
            self._last_voiced_end_ms = event.at_ms

    def on_partial(self, text: str, at_ms: float) -> None:
        """Feed an STT partial that arrived at ``at_ms``; blank ones and ones outside a turn are ignored."""
        if not self._turn_open or not text.strip():
            return
        self._partial = text
        if self._first_text_ms is None:
            self._first_text_ms = at_ms

    def adaptive_ms(self) -> float:
        """§6 rules 2-3: the per-caller silence before the completeness multiplier."""
        base = self._settings.base_ms
        if len(self._pauses) < MIN_PAUSE_SAMPLES:
            return base
        p90 = percentile(self._pauses, 90)
        return _clamp(max(base, 1.25 * p90 + 80), ADAPTIVE_MIN_MS, ADAPTIVE_MAX_MS)

    def timeout_ms(self) -> float:
        """Silence after the last voiced window that ends the turn, for the latest partial."""
        settings = self._settings
        if settings.interruption_threshold_ms is not None:
            timeout = float(settings.interruption_threshold_ms)
        else:
            multiplier = completeness_multiplier(self._partial, collecting_number=settings.collecting_number)
            timeout = _clamp(self.adaptive_ms() * multiplier, TIMEOUT_MIN_MS, TIMEOUT_MAX_MS)
        if settings.ivr_mode:
            timeout = max(timeout, IVR_FLOOR_MS)
        return timeout

    def text_wait_cap_ms(self) -> float:
        """Silence after which a turn that still has no text is discarded as noise."""
        return max(self.timeout_ms(), TIMEOUT_MAX_MS)

    def deadline_ms(self) -> float | None:
        """Next instant at which :meth:`poll` can return a result; ``None`` while idle or speaking."""
        if not self._turn_open or self._speaking:
            return None
        if self._first_text_ms is None:
            return self._last_voiced_end_ms + self.text_wait_cap_ms()
        return self._last_voiced_end_ms + self.timeout_ms()

    def poll(self, now_ms: float) -> EndOfTurn | TurnDiscarded | None:
        """End or discard the turn if its deadline has passed at ``now_ms``."""
        deadline = self.deadline_ms()
        if deadline is None or now_ms < deadline:
            return None
        result: EndOfTurn | TurnDiscarded
        if self._first_text_ms is None:
            result = TurnDiscarded(now_ms, self._started_ms, self._last_voiced_end_ms)
        else:
            timeout = self.timeout_ms()
            in_time = self._first_text_ms <= self._last_voiced_end_ms + timeout
            reason: EndOfTurnReason = "silence" if in_time else "late_transcript"
            result = EndOfTurn(now_ms, reason, self._last_voiced_end_ms, timeout, self._partial)
        self._close_turn()
        return result

    def _on_speech_start(self, at_ms: float) -> None:
        if self._turn_open:
            pause = at_ms - self._last_voiced_end_ms
            if PAUSE_MIN_MS <= pause <= PAUSE_MAX_MS:
                self._pauses.append(pause)
        else:
            self._turn_open = True
            self._started_ms = at_ms
        self._speaking = True

    def _close_turn(self) -> None:
        self._turn_open = False
        self._speaking = False
        self._started_ms = 0.0
        self._last_voiced_end_ms = 0.0
        self._partial = ""
        self._first_text_ms = None
