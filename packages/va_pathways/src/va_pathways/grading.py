"""Goal grading of pathway unit tests and simulations (specs/12 §8 step 2 and §9; Decisions P07-T11a and
P07-T11d there).

One forced ``grade`` call on the analysis model at the gateway's default temperature 0: the transcript goes
first, the spec prompt with the goal second. A unit-test case sends its history, input and the agent's reply
with :data:`GRADE_PROMPT`; a simulation sends the whole conversation, its final variables, and
:data:`SIMULATION_GRADE_PROMPT`. A gateway that cannot answer (policy exhausted, invalid output after the
repair) gives ``None``: the case or simulation reports ``grading_unavailable``.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from va_common.json import dumps_str
from va_common.logging import get_logger
from va_engine.decisions import StructuredPort
from va_llm import ChatMessage, LlmUnavailableError, StructuredOutputError
from va_llm.structured import register_structured_function

__all__ = [
    "GRADE_DESCRIPTION",
    "GRADE_PROMPT",
    "SIMULATION_GRADE_PROMPT",
    "GradeVerdict",
    "TranscriptLine",
    "grade",
    "grade_conversation",
    "grade_conversation_messages",
    "grade_messages",
]

GRADE_PROMPT: Final = (
    "Given the conversation and the agent's reply, does the reply satisfy this goal? "
    'Goal: "{goal}". Be strict.'
)
SIMULATION_GRADE_PROMPT: Final = (
    "Given the full conversation and the final variables, did the conversation achieve this goal? "
    'Goal: "{goal}". Be strict.'
)
GRADE_DESCRIPTION: Final = "Record whether the agent's reply satisfies the goal, with a short rationale."
_SPEAKERS: Final = {"user": "Caller", "assistant": "Agent"}

_log = get_logger(__name__)

type TranscriptLine = tuple[str, str]
"""``(role, text)`` with role ``user`` or ``assistant``."""


class GradeVerdict(BaseModel):
    """The ``grade`` function's arguments and the case's ``grading`` item (``{"pass", "rationale"}``)."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_name=True, validate_by_alias=True)

    pass_: bool = Field(validation_alias="pass", serialization_alias="pass")
    rationale: str

    def item(self) -> dict[str, bool | str]:
        return {"pass": self.pass_, "rationale": self.rationale}


register_structured_function("grade", GradeVerdict)


def _conversation(transcript: Sequence[TranscriptLine]) -> str:
    lines = "\n".join(f"{_SPEAKERS.get(role, role)}: {text}" for role, text in transcript)
    return f"Conversation:\n{lines}"


def grade_messages(transcript: Sequence[TranscriptLine], goal: str) -> list[ChatMessage]:
    """The transcript message, then the spec prompt with ``goal`` substituted literally."""
    return [
        ChatMessage(role="user", content=_conversation(transcript)),
        ChatMessage(role="user", content=GRADE_PROMPT.replace("{goal}", goal)),
    ]


def grade_conversation_messages(
    transcript: Sequence[TranscriptLine], variables: Mapping[str, JsonValue], goal: str
) -> list[ChatMessage]:
    """The transcript message, the final variables (JSON, sorted keys), then the simulation prompt with
    ``goal`` substituted literally."""
    return [
        ChatMessage(role="user", content=_conversation(transcript)),
        ChatMessage(role="user", content=f"Final variables: {dumps_str(dict(variables), sort_keys=True)}"),
        ChatMessage(role="user", content=SIMULATION_GRADE_PROMPT.replace("{goal}", goal)),
    ]


async def _verdict(
    port: StructuredPort,
    org_id: uuid.UUID,
    messages: list[ChatMessage],
    event: str,
    call_id: uuid.UUID | None = None,
) -> GradeVerdict | None:
    try:
        return await port.call_structured(
            "analysis",
            GradeVerdict,
            org_id=org_id,
            messages=messages,
            function_name="grade",
            description=GRADE_DESCRIPTION,
            call_id=call_id,
        )
    except (LlmUnavailableError, StructuredOutputError) as exc:
        _log.warning(event, error=type(exc).__name__)
        return None


async def grade(
    port: StructuredPort, *, org_id: uuid.UUID, transcript: Sequence[TranscriptLine], goal: str
) -> GradeVerdict | None:
    """The verdict on the transcript's last reply; ``None`` when the analysis model could not give one."""
    return await _verdict(
        port, org_id, grade_messages(transcript, goal), "pathway_unit_test.grading_unavailable"
    )


async def grade_conversation(  # noqa: PLR0913 - the conversation, its goal, and where it ran
    port: StructuredPort,
    *,
    org_id: uuid.UUID,
    transcript: Sequence[TranscriptLine],
    variables: Mapping[str, JsonValue],
    goal: str,
    call_id: uuid.UUID | None = None,
) -> GradeVerdict | None:
    """The verdict on a whole simulated conversation (``call_id`` = the conversation's id); ``None`` when the
    analysis model could not give one."""
    return await _verdict(
        port,
        org_id,
        grade_conversation_messages(transcript, variables, goal),
        "pathway_simulation.grading_unavailable",
        call_id,
    )
