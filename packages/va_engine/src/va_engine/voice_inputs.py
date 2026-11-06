"""Map a call onto the Voice System Prompt inputs (specs/10 §5, specs/11 §6; Decision P05-T09c).

``counterpart`` is the callee for outbound calls and the number the caller dialled (our agent number) for
inbound calls; web calls have none. Persona fields (agent name, company) arrive with personas (phase 13).
The context's dynamic data lines (specs/13 §7) become the prompt's ``## Context`` block: ``context_sections``
(the pre-call fetches), then the lines of the per-turn fetches (``turn_sections``: the caller's refreshed
lines, else the context's own, Decision P08-T03b).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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


def voice_prompt_inputs(  # noqa: PLR0913 - the prompt's per-request values, all keyword-only
    context: CallContext,
    *,
    now: datetime,
    instructions: str,
    variables: Mapping[str, object],
    memory_context: str | None = None,
    turn_sections: Sequence[str] | None = None,
) -> VoicePromptInputs:
    """The prompt inputs of ``context`` at ``now`` (aware) with the rendered ``instructions``;
    ``turn_sections`` are the current lines of the per-turn fetches (``None``: the context's)."""
    config = context.config
    turn_lines = context.turn_lines() if turn_sections is None else turn_sections
    return VoicePromptInputs(
        now_local=now.astimezone(call_zone(context)),
        timezone=config.timezone,
        direction=context.direction,
        language_name=language_name(config.language),
        instructions=instructions,
        counterpart=counterpart_of(context),
        variables=cast("Mapping[str, PromptValue]", variables),
        memory_context=memory_context,
        context_sections=(*context.context_sections, *turn_lines),
    )
