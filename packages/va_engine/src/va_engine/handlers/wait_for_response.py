"""Wait for Response node handler (specs/11 §4 "Wait for Response" row, §3.2.9, Decision P07-T06 rule 6).

``on_enter`` arms the wait timer (:class:`Wait`, ``state.wait_armed_at_ms``) and resets the reprompt count;
``respond`` speaks the node's ``text``/``prompt`` when it has one. When the timer expires without user input
the turn orchestration calls :meth:`WaitForResponseHandler.timeout`: voice reprompts with ``repromptText`` up
to ``maxReprompts`` times, then applies ``onTimeout`` (``end_call``, ``move_to_node`` with ``timeoutNodeId``,
or ``repeat``). User input disarms the timer (:func:`disarm`).

Text channels have no reprompts: the wait is conversation inactivity, armed only for ``onTimeout: end_call``
with ``waitSeconds * 10`` seconds, and its expiry closes the conversation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from va_common.logging import get_logger
from va_engine.handlers.base import HandlerResult, TurnContext, has_content, is_text, speak_node
from va_engine.models.nodes import PathwayNode, WaitForResponseConfig
from va_engine.models.results import Action, EndCallAction, Say, Wait
from va_engine.models.state import EngineState

__all__ = [
    "TEXT_WAIT_FACTOR",
    "TIMEOUT_END_REASON",
    "WaitForResponseHandler",
    "WaitTimeout",
    "disarm",
]

TIMEOUT_END_REASON: Final = "no_input_timeout"
TEXT_WAIT_FACTOR: Final = 10

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class WaitTimeout:
    """What a wait timeout does: ``result`` to execute, then move to ``move_to`` when it is set."""

    result: HandlerResult
    move_to: str | None = None


def disarm(state: EngineState) -> None:
    """Clear the wait timer (the user spoke or the node was left)."""
    state.wait_armed_at_ms = None


def _config(node: PathwayNode) -> WaitForResponseConfig:
    return node.data.wait_for_response or WaitForResponseConfig()


def _arm(ctx: TurnContext, seconds: float) -> Wait:
    ctx.state.wait_armed_at_ms = ctx.now_ms()
    return Wait(seconds=seconds)


def _end(ctx: TurnContext) -> WaitTimeout:
    disarm(ctx.state)
    ctx.state.ended = True
    ctx.state.end_reason = TIMEOUT_END_REASON
    action = EndCallAction(reason=TIMEOUT_END_REASON, ended_by="system")
    return WaitTimeout(HandlerResult((action,)))


class WaitForResponseHandler:
    """Wait for Response nodes."""

    async def on_enter(self, ctx: TurnContext) -> HandlerResult:
        """Reset the reprompts and arm the timer (text channels: only for ``onTimeout: end_call``)."""
        config = _config(ctx.node)
        ctx.state.wait_reprompts = 0
        if is_text(ctx.channel):
            if config.on_timeout != "end_call":
                disarm(ctx.state)
                return HandlerResult()
            return HandlerResult((_arm(ctx, config.wait_seconds * TEXT_WAIT_FACTOR),))
        return HandlerResult((_arm(ctx, config.wait_seconds),))

    async def respond(self, ctx: TurnContext) -> HandlerResult:
        """The node's ``text``/``prompt`` reply when it has one, else nothing."""
        return speak_node(ctx, prompt_required=True)

    def waits_for_user(self, node: PathwayNode) -> bool:
        """Always: the node exists to wait for the caller."""
        del node
        return True

    async def timeout(self, ctx: TurnContext) -> WaitTimeout:
        """The armed wait expired without user input."""
        config = _config(ctx.node)
        if is_text(ctx.channel):
            return _end(ctx)
        state = ctx.state
        if has_content(config.reprompt_text) and state.wait_reprompts < config.max_reprompts:
            reprompt = ctx.render(config.reprompt_text or "", field_name="repromptText").strip()
            if reprompt:
                state.wait_reprompts += 1
                say = Say(text=reprompt, mark=ctx.next_mark(), interruptible=None)
                return WaitTimeout(HandlerResult((say, _arm(ctx, config.wait_seconds))))
        if config.on_timeout == "move_to_node":
            target = config.timeout_node_id
            if target and ctx.graph.node(target) is not None:
                disarm(state)
                return WaitTimeout(HandlerResult(), move_to=target)
            _log.warning("engine.wait_timeout_target_missing", node_id=ctx.node.id)
            return _end(ctx)
        if config.on_timeout == "repeat":
            state.wait_reprompts = 0
            reply = await self.respond(ctx)
            actions: tuple[Action, ...] = (*reply.actions, _arm(ctx, config.wait_seconds))
            return WaitTimeout(HandlerResult(actions))
        return _end(ctx)
