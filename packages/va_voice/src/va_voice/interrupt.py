"""Interruptibility levels and backchannel handling (specs/07-media-pipeline.md §7, §13).

Everything here is pure and synchronous. The caller measures ``speech_ms`` (duration of the current
continuous VAD speech segment, i.e. now minus the last ``speech_start``) and passes the latest partial
transcript; the policy answers whether the agent's playback must be interrupted.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

from va_voice.text_norm import tokens

__all__ = [
    "ALWAYS",
    "BACKCHANNELS",
    "BALANCED",
    "BLOCK",
    "DEFAULT_LEVEL",
    "DIFFICULT",
    "EASY",
    "LEVEL_RULES",
    "MAX_CALL_LEVEL",
    "MAX_NODE_LEVEL",
    "BackchannelClassifier",
    "BackchannelMatch",
    "InterruptDecision",
    "InterruptLevel",
    "InterruptPolicy",
    "InterruptReason",
    "LevelRule",
    "effective_level",
]

InterruptLevel = Literal[0, 1, 2, 3, 4]
InterruptReason = Literal[
    "not_speaking", "blocked", "speech_and_words", "long_speech", "any_speech", "insufficient", "dtmf"
]

BLOCK: Final = 0
DIFFICULT: Final = 1
BALANCED: Final = 2
EASY: Final = 3
ALWAYS: Final = 4
DEFAULT_LEVEL: Final = BALANCED
MAX_CALL_LEVEL: Final = ALWAYS  # call `interruptibility` 0-4 (specs/05)
MAX_NODE_LEVEL: Final = EASY  # node `interruptibility` 0-3 (specs/11)

BACKCHANNELS: Final[tuple[str, ...]] = (
    "uh-huh",
    "uh huh",
    "mm-hmm",
    "mhm",
    "mm",
    "hmm",
    "yeah",
    "yep",
    "yes",
    "ok",
    "okay",
    "right",
    "sure",
    "got it",
    "i see",
    "alright",
    "all right",
    "oh",
    "ah",
    "cool",
    "great",
    "nice",
    "uh",
    "um",
    "wow",
    "true",
    "exactly",
    "perfect",
    "gotcha",
)


@dataclass(frozen=True, slots=True)
class LevelRule:
    """Thresholds of one level (specs/07 §7 table).

    Interrupt when ``speech_ms ≥ min_speech_ms`` and the word condition holds, or when
    ``speech_ms ≥ speech_only_ms`` regardless of words. ``min_total_words`` counts every token and
    additionally requires at least one non-backchannel token (level 1); ``min_content_words`` counts
    non-backchannel tokens (levels 2, 3).
    """

    min_speech_ms: float
    min_total_words: int = 0
    min_content_words: int = 0
    speech_only_ms: float | None = None


LEVEL_RULES: Final[Mapping[int, LevelRule]] = MappingProxyType(
    {
        DIFFICULT: LevelRule(min_speech_ms=1200, min_total_words=4),
        BALANCED: LevelRule(min_speech_ms=500, min_content_words=2, speech_only_ms=900),
        EASY: LevelRule(min_speech_ms=250, min_content_words=1, speech_only_ms=600),
        ALWAYS: LevelRule(min_speech_ms=150),
    }
)


def _check_level(level: int, maximum: int, what: str) -> None:
    if isinstance(level, bool) or not 0 <= level <= maximum:
        msg = f"{what} must be an integer 0-{maximum}, got {level!r}"
        raise ValueError(msg)


def _as_level(level: int) -> InterruptLevel:
    _check_level(level, MAX_CALL_LEVEL, "interruptibility")
    levels: tuple[InterruptLevel, ...] = (0, 1, 2, 3, 4)
    return levels[level]


def effective_level(
    *,
    call_level: int = DEFAULT_LEVEL,
    node_level: int | None = None,
    block_interruptions: bool = False,
    interruptible: bool | None = None,
) -> InterruptLevel:
    """Level in force for the current utterance.

    Order: ``block_interruptions`` → 0; ``interruptible is False`` (a non-interruptible ``Say``) → 0;
    the node level when set; else the call level. ``interruptible=True`` never raises the level.
    """
    _check_level(call_level, MAX_CALL_LEVEL, "call interruptibility")
    if node_level is not None:
        _check_level(node_level, MAX_NODE_LEVEL, "node interruptibility")
    if block_interruptions or interruptible is False:
        return 0
    return _as_level(call_level if node_level is None else node_level)


@dataclass(frozen=True, slots=True)
class BackchannelMatch:
    """Tokens of a partial and how many of them are covered by backchannel phrases."""

    words: tuple[str, ...]
    backchannel_words: int

    @property
    def total_words(self) -> int:
        return len(self.words)

    @property
    def content_words(self) -> int:
        """Tokens not covered by a backchannel phrase."""
        return self.total_words - self.backchannel_words

    @property
    def all_backchannel(self) -> bool:
        return self.total_words > 0 and self.content_words == 0


class BackchannelClassifier:
    """Greedy longest-phrase backchannel matching on normalized tokens (``uh-huh`` ≡ ``uh huh``)."""

    __slots__ = ("_max_len", "_phrases")

    def __init__(self, phrases: Iterable[str] = BACKCHANNELS) -> None:
        compiled: set[tuple[str, ...]] = set()
        for phrase in phrases:
            words = tokens(phrase)
            if not words:
                msg = f"backchannel phrase {phrase!r} has no words"
                raise ValueError(msg)
            compiled.add(words)
        if not compiled:
            msg = "at least one backchannel phrase is required"
            raise ValueError(msg)
        self._phrases: frozenset[tuple[str, ...]] = frozenset(compiled)
        self._max_len = max(len(words) for words in compiled)

    def classify(self, text_or_words: str | Sequence[str]) -> BackchannelMatch:
        """Count backchannel-covered tokens of a raw partial or of an already tokenized word list."""
        words = tokens(text_or_words) if isinstance(text_or_words, str) else _normalize_words(text_or_words)
        covered = 0
        index = 0
        while index < len(words):
            length = self._phrase_at(words, index)
            covered += length
            index += max(length, 1)
        return BackchannelMatch(words, covered)

    def is_backchannel(self, text_or_words: str | Sequence[str]) -> bool:
        """True when the partial is non-empty and made of backchannel phrases only."""
        return self.classify(text_or_words).all_backchannel

    def _phrase_at(self, words: tuple[str, ...], index: int) -> int:
        """Length of the longest phrase starting at ``index`` (0 when none)."""
        for length in range(min(self._max_len, len(words) - index), 0, -1):
            if words[index : index + length] in self._phrases:
                return length
        return 0


def _normalize_words(words: Sequence[str]) -> tuple[str, ...]:
    """STT word lists may carry punctuation or hyphens; split them like a raw partial."""
    return tuple(token for word in words for token in tokens(word))


@dataclass(frozen=True, slots=True)
class InterruptDecision:
    """Outcome of one evaluation; truthy when playback must be interrupted."""

    interrupt: bool
    reason: InterruptReason

    def __bool__(self) -> bool:
        return self.interrupt


_NOT_SPEAKING: Final = InterruptDecision(interrupt=False, reason="not_speaking")
_BLOCKED: Final = InterruptDecision(interrupt=False, reason="blocked")
_INSUFFICIENT: Final = InterruptDecision(interrupt=False, reason="insufficient")


def _words_ok(rule: LevelRule, match: BackchannelMatch) -> bool:
    if rule.min_total_words and (match.total_words < rule.min_total_words or match.all_backchannel):
        return False
    return match.content_words >= rule.min_content_words


def _apply_rule(
    rule: LevelRule, speech_ms: float, match: Callable[[], BackchannelMatch]
) -> InterruptDecision:
    if rule.speech_only_ms is not None and speech_ms >= rule.speech_only_ms:
        return InterruptDecision(interrupt=True, reason="long_speech")
    if speech_ms < rule.min_speech_ms:
        return _INSUFFICIENT
    if not (rule.min_total_words or rule.min_content_words):
        return InterruptDecision(interrupt=True, reason="any_speech")
    if _words_ok(rule, match()):
        return InterruptDecision(interrupt=True, reason="speech_and_words")
    return _INSUFFICIENT


class InterruptPolicy:
    """Evaluates the specs/07 §7 level table while the agent is speaking."""

    __slots__ = ("_classifier",)

    def __init__(self, classifier: BackchannelClassifier | None = None) -> None:
        self._classifier = classifier if classifier is not None else BackchannelClassifier()

    @property
    def classifier(self) -> BackchannelClassifier:
        return self._classifier

    def should_interrupt(
        self,
        speech_ms: float,
        words: Sequence[str] | None,
        text: str,
        level: int,
        agent_speaking: bool,
    ) -> InterruptDecision:
        """Decide whether the current user speech interrupts playback.

        ``words`` is the partial's word list when the STT provides one; ``None`` tokenizes ``text`` instead.
        Thresholds are inclusive.
        """
        checked = _as_level(level)
        if speech_ms < 0:
            msg = f"speech_ms must be ≥ 0, got {speech_ms!r}"
            raise ValueError(msg)
        if not agent_speaking:
            return _NOT_SPEAKING
        if checked == BLOCK:
            return _BLOCKED
        source: str | Sequence[str] = text if words is None else words
        return _apply_rule(LEVEL_RULES[checked], speech_ms, lambda: self._classifier.classify(source))

    def should_interrupt_dtmf(self, level: int, agent_speaking: bool) -> InterruptDecision:
        """A DTMF digit during playback interrupts at levels ≥ 1 (specs/07 §13)."""
        checked = _as_level(level)
        if not agent_speaking:
            return _NOT_SPEAKING
        if checked == BLOCK:
            return _BLOCKED
        return InterruptDecision(interrupt=True, reason="dtmf")
