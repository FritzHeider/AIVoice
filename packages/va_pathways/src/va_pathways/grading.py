"""The grading-goal assertion of a pathway unit test (specs/12 §8 step 2; Decision P07-T11a there).

One forced ``grade`` call on the analysis model at the gateway's default temperature 0: the transcript
(history, the case input and the agent's reply) goes first, the spec prompt with the goal second. A gateway
that cannot answer (policy exhausted, invalid output after the repair) gives ``None``: the case reports
``grading_unavailable``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from va_common.logging import get_logger
from va_engine.decisions import StructuredPort
from va_llm import ChatMessage, LlmUnavailableError, StructuredOutputError
from va_llm.structured import register_structured_function

__all__ = [
    "GRADE_DESCRIPTION",
    "GRADE_PROMPT",
    "GradeVerdict",
    "TranscriptLine",
    "grade",
    "grade_messages",
]

GRADE_PROMPT: Final = (
    "Given the conversation and the agent's reply, does the reply satisfy this goal? "
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


def grade_messages(transcript: Sequence[TranscriptLine], goal: str) -> list[ChatMessage]:
    """The transcript message, then the spec prompt with ``goal`` substituted literally."""
    lines = "\n".join(f"{_SPEAKERS.get(role, role)}: {text}" for role, text in transcript)
    return [
        ChatMessage(role="user", content=f"Conversation:\n{lines}"),
        ChatMessage(role="user", content=GRADE_PROMPT.replace("{goal}", goal)),
    ]


async def grade(
    port: StructuredPort, *, org_id: uuid.UUID, transcript: Sequence[TranscriptLine], goal: str
) -> GradeVerdict | None:
    """The verdict on the transcript's last reply; ``None`` when the analysis model could not give one."""
    try:
        return await port.call_structured(
            "analysis",
            GradeVerdict,
            org_id=org_id,
            messages=grade_messages(transcript, goal),
            function_name="grade",
            description=GRADE_DESCRIPTION,
        )
    except (LlmUnavailableError, StructuredOutputError) as exc:
        _log.warning("pathway_unit_test.grading_unavailable", error=type(exc).__name__)
        return None
