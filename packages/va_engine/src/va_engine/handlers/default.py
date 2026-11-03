"""Default node (and every global node) handler (specs/11 §4 "Default" and "Global" rows, Decision P07-T06).

``respond`` speaks the node's ``text`` (rendered) or streams the dialogue reply. Global nodes use the same
handler: the :class:`~va_engine.navigation.Navigator` already pushed the return frame when it entered the
node. Text channels behave the same (the reply is sent as a message).
"""

from __future__ import annotations

from va_engine.handlers.base import HandlerResult, TurnContext, speak_node
from va_engine.models.nodes import PathwayNode

__all__ = ["DefaultHandler"]


class DefaultHandler:
    """Default / Global nodes."""

    async def on_enter(self, ctx: TurnContext) -> HandlerResult:
        """Nothing happens when a Default node becomes current."""
        del ctx
        return HandlerResult()

    async def respond(self, ctx: TurnContext) -> HandlerResult:
        """``Say(render(text))`` for a non-blank ``text``, else a :class:`SayStream` of the dialogue reply."""
        return speak_node(ctx, prompt_required=False)

    def waits_for_user(self, node: PathwayNode) -> bool:
        """Yes, unless ``modelOptions.skipUserResponse``."""
        return not node.data.model_options.skip_user_response
