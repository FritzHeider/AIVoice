"""End Call node handler (specs/11 §4 "End Call" row, §3.2.3, Decision P07-T06 rules 3-4).

``respond`` speaks the final line (static ``speech`` or composed from ``speechPrompt``), then ends the call
with ``reason="agent_end_call"`` once that line played. Text channels send the line as the last message and
close the conversation.
"""

from __future__ import annotations

from typing import Final

from va_engine.handlers.base import HandlerResult, TurnContext, final_line, line_actions
from va_engine.models.nodes import PathwayNode
from va_engine.models.results import EndCallAction

__all__ = ["END_REASON", "EndCallHandler"]

END_REASON: Final = "agent_end_call"


class EndCallHandler:
    """End Call nodes."""

    async def on_enter(self, ctx: TurnContext) -> HandlerResult:
        """Nothing happens on entry; the final line comes from :meth:`respond`."""
        del ctx
        return HandlerResult()

    async def respond(self, ctx: TurnContext) -> HandlerResult:
        """The final line (if any), then :class:`EndCallAction` after its playback; marks the state ended."""
        config = ctx.node.data.end_call
        line = await final_line(
            ctx,
            speech=config.speech if config else None,
            speech_mode=config.speech_mode if config else "static",
            speech_prompt=config.speech_prompt if config else None,
        )
        speech, mark = line_actions(ctx, line)
        ctx.state.ended = True
        ctx.state.end_reason = END_REASON
        return HandlerResult((*speech, EndCallAction(reason=END_REASON, ended_by="agent", after_mark=mark)))

    def waits_for_user(self, node: PathwayNode) -> bool:
        """Never: the call ends."""
        del node
        return False
