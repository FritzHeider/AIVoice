"""Map a call onto the Voice System Prompt inputs (specs/10 §5, specs/11 §6; Decision P05-T09c).

``counterpart`` is the callee for outbound calls and the number the caller dialled (our agent number) for
inbound calls; web calls have none. Persona fields (agent name, company) arrive with personas (phase 13).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import cast

from va_domain.call_context import CallContext
from va_domain.languages import language_name
from va_engine.builtins import call_zone
from va_llm.prompts import PromptValue, VoicePromptInputs

__all__ = ["counterpart_of", "voice_prompt_inputs"]


def counterpart_of(context: CallContext) -> str:
    """The prompt's other party: the callee (outbound), our dialled number (inbound), none (web)."""
    return "" if context.direction == "web" else context.to_number


def voice_prompt_inputs(
    context: CallContext,
    *,
    now: datetime,
    instructions: str,
    variables: Mapping[str, object],
    memory_context: str | None = None,
) -> VoicePromptInputs:
    """The prompt inputs of ``context`` at ``now`` (aware) with the rendered ``instructions``."""
    config = context.config
    return VoicePromptInputs(
        now_local=now.astimezone(call_zone(context)),
        timezone=config.timezone,
        direction=context.direction,
        language_name=language_name(config.language),
        instructions=instructions,
        counterpart=counterpart_of(context),
        variables=cast("Mapping[str, PromptValue]", variables),
        memory_context=memory_context,
    )
