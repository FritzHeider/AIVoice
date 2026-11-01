"""Values exchanged between the call pipeline, the Brain and observers (specs/07-media-pipeline.md §8, §16).

Brain outputs: :class:`SpeakText` (streamed LLM deltas), :class:`SpeakStatic` (fixed text), :class:`EndCall`
(optional final line, then the pipeline ends) and :class:`BrainAction` (opaque telephony work handed to the
:class:`~va_voice.pipeline.protocols.ActionHandler`; va_media subclasses it for transfers, SMS, DTMF, ...).
"""

from __future__ import annotations

from collections.abc import AsyncIterable
from dataclasses import dataclass, field
from typing import Literal

from va_voice.endpointing import ResumptionSpeed

__all__ = [
    "AgentTurn",
    "BrainAction",
    "BrainOutput",
    "BrainTiming",
    "EndCall",
    "EndReason",
    "PipelineResult",
    "PipelineStats",
    "SpeakStatic",
    "SpeakText",
    "UserTurn",
]

EndReason = Literal["transport_closed", "agent_hangup", "max_duration", "no_input_timeout", "error"]
"""End reasons the pipeline produces itself (an :class:`EndCall` may carry any other reason)."""


@dataclass(frozen=True, slots=True)
class UserTurn:
    """A finished user turn on the pipeline timeline (ms); ``dtmf`` is set for keypad turns (§13)."""

    index: int
    text: str
    start_ms: float
    end_ms: float
    words: tuple[str, ...] = ()
    language_code: str | None = None
    dtmf: str | None = None


@dataclass(frozen=True, slots=True)
class SpeakText:
    """Speak streamed text deltas (LLM tokens) as they arrive.

    ``llm_request_ms``: when the Brain sent the LLM request (pipeline timeline); without it the request is
    marked when the pipeline starts reading ``tokens``.
    """

    tokens: AsyncIterable[str]
    interruptible: bool = True
    llm_request_ms: float | None = None


@dataclass(frozen=True, slots=True)
class SpeakStatic:
    """Speak a fixed text (opener, filler, reprompt, warning)."""

    text: str
    interruptible: bool = True


@dataclass(frozen=True, slots=True)
class EndCall:
    """End the call after speaking ``final_text`` (when given) to completion."""

    final_text: str | None = None
    reason: str = "agent_hangup"


@dataclass(frozen=True, slots=True)
class BrainAction:
    """A telephony action the pipeline passes through to its action handler."""

    kind: str


type BrainOutput = SpeakText | SpeakStatic | EndCall | BrainAction


@dataclass(frozen=True, slots=True)
class BrainTiming:
    """Timing settings of the Brain's current node (§6, §7, §12)."""

    interruptibility: int | None = None
    resumption_speed: ResumptionSpeed = 2
    collecting_number: bool = False
    no_input_prompt: str | None = None


@dataclass(frozen=True, slots=True)
class AgentTurn:
    """One agent utterance: the planned original ``text`` and the part the caller heard."""

    index: int
    text: str
    text_spoken: str
    interrupted: bool
    started_ms: float
    ended_ms: float


@dataclass(slots=True)
class PipelineStats:
    """Counters of one pipeline run."""

    user_turns: int = 0
    agent_turns: int = 0
    discarded_turns: int = 0
    commit_timeouts: int = 0
    stale_tts_chunks: int = 0


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """How the pipeline ended; ``turns`` = user + agent turns recorded."""

    end_reason: str
    turns: int
    stats: PipelineStats = field(default_factory=PipelineStats)
    error: BaseException | None = None
