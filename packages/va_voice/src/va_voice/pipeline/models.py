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
    "InterruptRecord",
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
    """A finished user turn on the pipeline timeline (ms); ``dtmf`` is set for keypad turns (§13).

    ``supersedes``: index of an earlier turn this one replaces (§8 concurrency rule: the earlier turn's reply
    was cancelled before any audio and its text is the front of this one).
    """

    index: int
    text: str
    start_ms: float
    end_ms: float
    words: tuple[str, ...] = ()
    language_code: str | None = None
    dtmf: str | None = None
    supersedes: int | None = None


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
    """Timing settings of the Brain's current node (§6, §7, §12).

    ``no_input_timeout_ms``: the Brain's own wait (a pathway Wait for Response node, Decision P07-T12a). When
    set the no-input timer uses it instead of ``no_input_timeout_ms`` of the config, and its expiry goes to
    ``Brain.on_no_input`` without counting toward ``max_reprompts`` (no default reprompt, no goodbye).
    """

    interruptibility: int | None = None
    resumption_speed: ResumptionSpeed = 2
    collecting_number: bool = False
    no_input_prompt: str | None = None
    no_input_timeout_ms: int | None = None


@dataclass(frozen=True, slots=True)
class AgentTurn:
    """One agent utterance: the planned original ``text`` and the part the caller heard."""

    index: int
    text: str
    text_spoken: str
    interrupted: bool
    started_ms: float
    ended_ms: float


@dataclass(frozen=True, slots=True)
class InterruptRecord:
    """One honored barge-in (``call.interrupt``, §7 step 7), times in pipeline ms.

    ``reaction_ms`` = ``clear_ms - speech_start_ms``; ``clear_delay_ms`` = ``clear_ms - at_ms`` (decision to
    clear).
    """

    turn_index: int
    at_ms: float
    speech_start_ms: float
    clear_ms: float
    reaction_ms: float
    clear_delay_ms: float
    played_ms: float
    played_chars: int
    total_chars: int
    level: int
    reason: str


@dataclass(slots=True)
class PipelineStats:
    """Counters of one pipeline run."""

    user_turns: int = 0
    agent_turns: int = 0
    discarded_turns: int = 0
    commit_timeouts: int = 0
    stale_tts_chunks: int = 0
    barge_ins: int = 0
    unhonored_barge_ins: int = 0
    false_barge_ins: int = 0
    ignored_backchannels: int = 0
    merged_turns: int = 0
    held_turns: int = 0
    no_input_reprompts: int = 0
    dtmf_turns: int = 0
    ignored_dtmf: int = 0
    greeting_turns: int = 0
    greeting_timeouts: int = 0


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """How the pipeline ended; ``turns`` = user + agent turns recorded."""

    end_reason: str
    turns: int
    stats: PipelineStats = field(default_factory=PipelineStats)
    error: BaseException | None = None
