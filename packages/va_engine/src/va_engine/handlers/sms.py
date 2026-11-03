"""SMS node handler (specs/11 §4 "SMS" row, §3.2.4; Decision P07-T07 under specs/11 §4).

``on_enter`` builds the message: ``messageMode: "prompt"`` with a non-blank ``messagePrompt`` composes it
through the :class:`~va_engine.handlers.base.LinePort` (falling back to the rendered ``message``,
``engine.sms_message_fallback``), otherwise the rendered ``message``.

* ``sms`` channel: the message is simply the next message of the conversation (:class:`Say`); nothing is sent
  through the port.
* Other channels: ``to`` (default ``{{from}}``) is rendered and must be E.164; ``from`` rendered (blank = the
  call's agent number). The SMS goes out through the :class:`~va_engine.ports.SmsPort`. On success the
  rendered ``speech`` is said; on failure nothing is said (it would claim a text was sent), the problem is
  ``sms_failed``, and without ``continueOnFailure`` the conversation takes the node's fallback edge
  (``route_fallback``).

There is no ``SendSmsAction``: the node needs the send result to decide its route. Phone numbers are never
logged.
"""

from __future__ import annotations

from typing import Final

from va_common.logging import get_logger
from va_engine.handlers.base import (
    HandlerResult,
    LineUnavailableError,
    TurnContext,
    has_content,
    interruptible_of,
    speak_node,
)
from va_engine.handlers.reports import SmsReport
from va_engine.handlers.transfer_call import e164_number
from va_engine.handlers.webhook import node_waits
from va_engine.models.nodes import PathwayNode, SmsConfig
from va_engine.models.results import Action, Say
from va_engine.ports import SmsOutcome, SmsRequest

__all__ = ["MESSAGE_MISSING", "SMS_FAILED", "SMS_UNAVAILABLE", "SmsHandler"]

MESSAGE_MISSING: Final = "sms_message_missing"
SMS_FAILED: Final = "sms_failed"
SMS_UNAVAILABLE: Final = "sms_unavailable"

_log = get_logger(__name__)


async def _composed(ctx: TurnContext, config: SmsConfig) -> str | None:
    instruction = ctx.render(config.message_prompt or "", field_name="sms.messagePrompt").strip()
    if not instruction:
        _log.info("engine.sms_message_fallback", node_id=ctx.node.id, reason="blank_instruction")
        return None
    try:
        message = (await ctx.lines.compose(ctx, instruction)).strip()
    except LineUnavailableError as exc:
        _log.warning("engine.sms_message_fallback", node_id=ctx.node.id, reason=exc.reason)
        return None
    if not message:
        _log.warning("engine.sms_message_fallback", node_id=ctx.node.id, reason="blank")
    return message or None


async def _message(ctx: TurnContext, config: SmsConfig) -> str | None:
    if config.message_mode == "prompt" and has_content(config.message_prompt):
        composed = await _composed(ctx, config)
        if composed is not None:
            return composed
    return ctx.render(config.message, field_name="sms.message").strip() or None


def _failed(ctx: TurnContext, config: SmsConfig, outcome: SmsOutcome) -> HandlerResult:
    _log.warning("engine.sms_failed", node_id=ctx.node.id, error=outcome.error)
    report = SmsReport(node_id=ctx.node.id, sent=False, error=outcome.error)
    return HandlerResult(problem=SMS_FAILED, route_fallback=not config.continue_on_failure, reports=(report,))


class SmsHandler:
    """SMS nodes."""

    async def on_enter(self, ctx: TurnContext) -> HandlerResult:
        """Build and send the message (or, in an SMS conversation, send it as the next message)."""
        config = ctx.node.data.sms or SmsConfig()
        message = await _message(ctx, config)
        if message is None:
            _log.warning("engine.sms_message_missing", node_id=ctx.node.id)
            return HandlerResult(problem=MESSAGE_MISSING, route_fallback=not config.continue_on_failure)
        if ctx.channel == "sms":
            return HandlerResult((Say(text=message, mark=ctx.next_mark(), interruptible=None),))
        if ctx.sms is None:
            _log.warning("engine.sms_unavailable", node_id=ctx.node.id)
            return HandlerResult(problem=SMS_UNAVAILABLE, route_fallback=not config.continue_on_failure)
        to = e164_number(ctx.render(config.to, field_name="sms.to"))
        if to is None:
            return _failed(ctx, config, SmsOutcome(sent=False, error="invalid_to"))
        sender = ctx.render(config.from_, field_name="sms.from").strip() if config.from_ else ""
        request = SmsRequest(to=to, from_=sender or None, body=message, node_id=ctx.node.id)
        outcome = await ctx.sms.send(request)
        if not outcome.sent:
            return _failed(ctx, config, outcome)
        report = SmsReport(node_id=ctx.node.id, sent=True, message_id=outcome.message_id)
        actions: tuple[Action, ...] = ()
        speech = ctx.render(config.speech, field_name="sms.speech").strip() if config.speech else ""
        if speech:
            actions = (Say(text=speech, mark=ctx.next_mark(), interruptible=interruptible_of(ctx.node)),)
        return HandlerResult(actions, reports=(report,))

    async def respond(self, ctx: TurnContext) -> HandlerResult:
        """The ``text``/``prompt`` reply; nothing without either (the node routes at once)."""
        return speak_node(ctx, prompt_required=True)

    def waits_for_user(self, node: PathwayNode) -> bool:
        """Only when the node has a reply (``text``/``prompt``) and not ``skipUserResponse``."""
        return node_waits(node)
