"""Press Button node handler (specs/11 §4 "Press Button" row, §3.2.5; Decision P07-T07 under specs/11 §4).

Voice channels (``voice``, ``widget_voice``): the optional rendered ``speech``, then
:class:`~va_engine.models.results.SendDtmfAction` once that speech played. Text channels ignore the node
(``engine.press_button_ignored``). The node never waits for the user: it routes at once.
"""

from __future__ import annotations

from va_common.logging import get_logger
from va_engine.handlers.base import HandlerResult, TurnContext, interruptible_of, is_text
from va_engine.models.nodes import PathwayNode
from va_engine.models.results import Action, Say, SendDtmfAction

__all__ = ["PressButtonHandler"]

_log = get_logger(__name__)


class PressButtonHandler:
    """Press Button nodes."""

    async def on_enter(self, ctx: TurnContext) -> HandlerResult:
        """Speech (optional) then the DTMF digits; nothing on text channels."""
        config = ctx.node.data.press_button
        if is_text(ctx.channel) or config is None:
            _log.warning(
                "engine.press_button_ignored",
                node_id=ctx.node.id,
                reason="text_channel" if config is not None else "no_config",
            )
            return HandlerResult()
        speech: tuple[Action, ...] = ()
        mark: str | None = None
        text = ctx.render(config.speech, field_name="pressButton.speech").strip() if config.speech else ""
        if text:
            mark = ctx.next_mark()
            speech = (Say(text=text, mark=mark, interruptible=interruptible_of(ctx.node)),)
        dtmf = SendDtmfAction(digits=config.digits, duration_ms=config.duration_millis, after_mark=mark)
        return HandlerResult((*speech, dtmf))

    async def respond(self, ctx: TurnContext) -> HandlerResult:
        """Nothing: the node routes at once."""
        del ctx
        return HandlerResult()

    def waits_for_user(self, node: PathwayNode) -> bool:
        """Never."""
        del node
        return False
