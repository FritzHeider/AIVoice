"""Per-call settings of the call pipeline (specs/07-media-pipeline.md §6-§13, §18; specs/05 call fields).

The caller (va_media's ``CallSession``) resolves call, agent and org settings into one
:class:`PipelineConfig`: ``max_duration_s`` is the Bland ``max_duration`` (minutes) x 60; the spoken
defaults equal the org settings defaults of specs/04 §7 (``va_domain.org.OrgSettings``), which the caller
overrides with the org's values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from va_voice.audio import L16_16000, PCM16LE_16000, ULAW_8000, AudioFormat, L16ByteOrder
from va_voice.endpointing import THRESHOLD_MAX_MS, THRESHOLD_MIN_MS
from va_voice.text.pronunciation import PronunciationRuleLike

__all__ = [
    "DEFAULT_MAX_DURATION_MESSAGE",
    "DEFAULT_NO_INPUT_GOODBYE",
    "DEFAULT_NO_INPUT_PROMPT",
    "NO_INPUT_TIMEOUT_MAX_MS",
    "NO_INPUT_TIMEOUT_MIN_MS",
    "SUPPORTED_FORMATS",
    "PipelineConfig",
]

DEFAULT_MAX_DURATION_MESSAGE: Final = "I'm sorry, we've reached the time limit for this call. Goodbye."
"""``org.settings.max_duration_message`` default (specs/04 §7)."""

DEFAULT_NO_INPUT_GOODBYE: Final = "I haven't heard anything, so I'll end the call now. Goodbye."
"""``org.settings.no_input_goodbye`` default (specs/04 §7)."""

DEFAULT_NO_INPUT_PROMPT: Final = "Are you still there?"
"""Reprompt when the node has no ``noInputPrompt`` (specs/07 §12)."""

NO_INPUT_TIMEOUT_MIN_MS: Final = 3000
NO_INPUT_TIMEOUT_MAX_MS: Final = 30_000
MAX_INTERRUPTIBILITY: Final = 4
_THRESHOLDS: Final = f"{THRESHOLD_MIN_MS}..{THRESHOLD_MAX_MS}"

SUPPORTED_FORMATS: Final = (ULAW_8000, L16_16000, PCM16LE_16000)
"""Leg (wire) formats: PCMU 8 kHz, L16 16 kHz (hd_audio, ``l16_byte_order``), LE PCM16 16 kHz (widget)."""


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    """Settings of one call leg's pipeline; invalid values raise ``ValueError``."""

    fmt: AudioFormat = ULAW_8000
    l16_byte_order: L16ByteOrder = "big"
    interruptibility: int = 2
    block_interruptions: bool = False
    interruption_threshold_ms: int | None = None
    ivr_mode: bool = False
    max_duration_s: float = 1800.0
    max_duration_message: str = DEFAULT_MAX_DURATION_MESSAGE
    max_duration_warning_s: float = 10.0
    stt_commit_timeout_ms: int = 1500
    final_line_timeout_s: float = 8.0
    no_input_timeout_ms: int = 8000
    max_reprompts: int = 2
    no_input_prompt: str = DEFAULT_NO_INPUT_PROMPT
    no_input_goodbye: str = DEFAULT_NO_INPUT_GOODBYE
    ignore_button_press: bool = False
    pronunciation: tuple[PronunciationRuleLike, ...] = ()
    reorder_window: int = 5

    def __post_init__(self) -> None:
        for problem in self._problems():
            raise ValueError(problem)

    def _problems(self) -> list[str]:
        checks = (
            (self.fmt in SUPPORTED_FORMATS, f"unsupported leg format {self.fmt.name}"),
            (0 <= self.interruptibility <= MAX_INTERRUPTIBILITY, "interruptibility must be within 0..4"),
            (self._threshold_ok(), f"interruption_threshold_ms must be within {_THRESHOLDS}"),
            (self.max_duration_s > 0, "max_duration_s must be > 0"),
            (
                0 <= self.max_duration_warning_s < self.max_duration_s,
                "max_duration_warning_s must be >= 0 and below max_duration_s",
            ),
            (self.stt_commit_timeout_ms > 0, "stt_commit_timeout_ms must be > 0"),
            (self.final_line_timeout_s > 0, "final_line_timeout_s must be > 0"),
            (
                NO_INPUT_TIMEOUT_MIN_MS <= self.no_input_timeout_ms <= NO_INPUT_TIMEOUT_MAX_MS,
                f"no_input_timeout_ms must be within {NO_INPUT_TIMEOUT_MIN_MS}..{NO_INPUT_TIMEOUT_MAX_MS}",
            ),
            (self.max_reprompts >= 0, "max_reprompts must be >= 0"),
            (self.reorder_window >= 1, "reorder_window must be >= 1"),
        )
        return [message for ok, message in checks if not ok]

    def _threshold_ok(self) -> bool:
        threshold = self.interruption_threshold_ms
        return threshold is None or THRESHOLD_MIN_MS <= threshold <= THRESHOLD_MAX_MS

    @property
    def tts_format(self) -> AudioFormat:
        """The TTS output format for the leg: ``ulaw_8000`` on PCMU legs, ``pcm_16000`` on 16 kHz legs."""
        return ULAW_8000 if self.fmt == ULAW_8000 else PCM16LE_16000

    @property
    def wire_byte_order(self) -> L16ByteOrder:
        """Byte order of 16 kHz wire samples (little-endian for ``pcm_16000`` legs)."""
        return "little" if self.fmt == PCM16LE_16000 else self.l16_byte_order
