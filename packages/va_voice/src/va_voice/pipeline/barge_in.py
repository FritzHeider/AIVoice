"""Barge-in bookkeeping of one leg (specs/07-media-pipeline.md §7, §8; Decision P04-T12b).

:class:`BargeInMonitor` is pure and synchronous. It follows the caller's current continuous VAD speech
segment and the latest STT partial of the open user turn, and evaluates the
:class:`~va_voice.interrupt.InterruptPolicy` whenever the pipeline asks (after every inbound frame batch and
every partial). An attempt (speech overlapping playback) that ends without being honored, because the
segment or the playback ended, is reported once through ``on_unhonored``; a segment that outlives one
utterance is evaluated again against the next one (§7 "evaluated continuously"), only an honored decision
ends its evaluation. :func:`merge_turns` joins two user turns (concurrency rule, held turns).
"""

from __future__ import annotations

from collections.abc import Callable

from va_voice.interrupt import BackchannelClassifier, InterruptDecision, InterruptPolicy
from va_voice.pipeline.models import UserTurn
from va_voice.vad import VadEvent

__all__ = ["BargeInMonitor", "merge_turns"]


def _ignore() -> None:
    return None


class BargeInMonitor:
    """The caller's speech as the interruption policy sees it."""

    __slots__ = (
        "_attempt",
        "_decided",
        "_on_unhonored",
        "_overlapped",
        "_partial",
        "_policy",
        "_speech_start_ms",
    )

    def __init__(
        self, policy: InterruptPolicy | None = None, *, on_unhonored: Callable[[], None] = _ignore
    ) -> None:
        self._policy = policy if policy is not None else InterruptPolicy()
        self._on_unhonored = on_unhonored
        self._speech_start_ms: float | None = None
        self._partial = ""
        self._attempt = False
        self._decided = False
        self._overlapped = False

    @property
    def classifier(self) -> BackchannelClassifier:
        return self._policy.classifier

    @property
    def speech_started_ms(self) -> float | None:
        """``speech_start`` of the open VAD segment (``None`` while the caller is silent)."""
        return self._speech_start_ms

    @property
    def partial(self) -> str:
        """Latest partial of the open user turn."""
        return self._partial

    @property
    def overlapped_playback(self) -> bool:
        """The open user turn overlapped interruptible playback (level ≥ 1) without a barge-in."""
        return self._overlapped

    def on_vad(self, event: VadEvent) -> None:
        """``speech_start`` opens a segment; ``speech_end`` closes it (unhonored if it overlapped)."""
        if event.kind == "speech_start" and self._speech_start_ms is None:
            self._speech_start_ms = event.at_ms
            self._attempt = False
            self._decided = False
        elif event.kind == "speech_end" and self._speech_start_ms is not None:
            self._close_attempt()
            self._speech_start_ms = None

    def on_partial(self, text: str) -> None:
        if text.strip():
            self._partial = text

    def reset_turn(self) -> None:
        """The user turn ended (or was discarded): forget its partial and its overlap."""
        self._partial = ""
        self._overlapped = False

    def evaluate(self, now_ms: float, *, playing: bool, level: int) -> InterruptDecision | None:
        """A truthy decision (once per segment) when the caller's speech interrupts playback at ``level``.

        While nothing plays an open attempt is closed as unhonored, but the segment stays open for evaluation:
        the same speech may still interrupt the next utterance.
        """
        start = self._speech_start_ms
        if not playing:
            self._close_attempt()
            return None
        if start is None or self._decided:
            return None
        self._attempt = True
        if level >= 1:
            self._overlapped = True
        speech_ms = max(0.0, now_ms - start)
        decision = self._policy.should_interrupt(speech_ms, None, self._partial, level, agent_speaking=True)
        if not decision:
            return None
        self._decided = True
        self._attempt = False
        self._overlapped = False
        return decision

    def evaluate_dtmf(self, *, playing: bool, level: int) -> InterruptDecision | None:
        """A truthy decision when a DTMF digit interrupts playback at ``level`` (§13: levels ≥ 1)."""
        decision = self._policy.should_interrupt_dtmf(level, agent_speaking=playing)
        return decision if decision else None

    def _close_attempt(self) -> None:
        if self._attempt:
            self._attempt = False
            self._on_unhonored()


def merge_turns(prev: UserTurn | None, new: UserTurn, *, supersede: bool = False) -> UserTurn:
    """``new`` with ``prev`` in front: text ``"{prev} {new}"``, the earlier start, both word lists.

    ``dtmf`` joins both keypad parts (``None`` when neither has digits).
    ``supersede``: ``prev`` was already given to the Brain, which replaces it with the merged turn
    (``supersedes`` = ``prev.index``). A missing or blank ``prev`` leaves ``new`` unchanged.
    """
    if prev is None or not prev.text.strip():
        return new
    return UserTurn(
        index=new.index,
        text=f"{prev.text.strip()} {new.text.strip()}".strip(),
        start_ms=min(prev.start_ms, new.start_ms),
        end_ms=new.end_ms,
        words=prev.words + new.words,
        language_code=new.language_code if new.language_code is not None else prev.language_code,
        dtmf=_join_dtmf(prev.dtmf, new.dtmf),
        supersedes=prev.index if supersede else new.supersedes,
    )


def _join_dtmf(prev: str | None, new: str | None) -> str | None:
    if prev is None:
        return new
    return prev if new is None else prev + new
