"""Transfer Call node handler (specs/11 §4 "Transfer Call" row, §3.2.2, Decision P07-T06 rules 3 and 5).

Voice: speak the final line, then hand over a :class:`TransferAction` that runs once the line played. The
target is the rendered ``transfer.number`` (legacy ``transferNumber``); a key of the call's ``transfer_list``
resolves to its number. Only E.164 numbers and ``sip:``/``sips:`` URIs are accepted: anything else speaks the
pathway's ``errorPhrase`` and reports ``transfer_target_invalid`` (the number is never logged). Warm settings
pass through; phase 12 executes warm transfers.

SMS: send ``transfer.textModeMessage`` (default :data:`DEFAULT_TEXT_MODE_MESSAGE`) and hand the conversation
to a human; widget channels hand over at once.
"""

from __future__ import annotations

import re
from typing import Final

from va_common.logging import get_logger
from va_engine.handlers.base import (
    HandlerResult,
    TurnContext,
    final_line,
    has_content,
    line_actions,
)
from va_engine.models.nodes import PathwayNode, effective_transfer_number
from va_engine.models.results import Action, HumanTakeover, Say, TransferAction

__all__ = [
    "DEFAULT_TEXT_MODE_MESSAGE",
    "TARGET_INVALID",
    "TransferCallHandler",
    "resolve_transfer_target",
]

DEFAULT_TEXT_MODE_MESSAGE: Final = "A team member will reach out shortly."
TARGET_INVALID: Final = "transfer_target_invalid"

_E164: Final = re.compile(r"^\+[1-9]\d{6,14}$")
_SIP_URI: Final = re.compile(r"^sips?:\S+$", re.IGNORECASE)
_SEPARATORS: Final = re.compile(r"[\s().-]")

_log = get_logger(__name__)


def _target_of(value: str) -> str | None:
    if _SIP_URI.match(value):
        return value
    if value.startswith("+"):
        number = _SEPARATORS.sub("", value)
        if _E164.match(number):
            return number
    return None


def resolve_transfer_target(ctx: TurnContext) -> str | None:
    """The node's transfer target as E.164 (separators removed) or a SIP URI; ``None`` if missing/invalid.

    The configured value is rendered first; a value naming a ``transfer_list`` key is replaced by its number.
    """
    configured = effective_transfer_number(ctx.node.data)
    if configured is None:
        return None
    value = ctx.render(configured, field_name="transfer.number").strip()
    listed = ctx.transfer_list.get(value)
    if listed is not None:
        value = listed.strip()
    return _target_of(value)


def _text_mode_message(ctx: TurnContext) -> str:
    config = ctx.node.data.transfer
    extra = config.model_extra if config is not None else None
    custom = extra.get("textModeMessage") if extra else None
    if isinstance(custom, str) and has_content(custom):
        rendered = ctx.render(custom, field_name="transfer.textModeMessage").strip()
        if rendered:
            return rendered
    return DEFAULT_TEXT_MODE_MESSAGE


class TransferCallHandler:
    """Transfer Call nodes (cold; warm settings are carried for phase 12)."""

    async def on_enter(self, ctx: TurnContext) -> HandlerResult:
        """Nothing happens on entry; the transfer comes from :meth:`respond`."""
        del ctx
        return HandlerResult()

    async def respond(self, ctx: TurnContext) -> HandlerResult:
        """The channel's transfer: final line + :class:`TransferAction` (voice), message + human takeover
        (SMS), human takeover (widget)."""
        if ctx.channel == "sms":
            mark = ctx.next_mark()
            message = Say(text=_text_mode_message(ctx), mark=mark, interruptible=False)
            return HandlerResult((message, HumanTakeover(after_mark=mark)))
        if ctx.channel != "voice":
            return HandlerResult((HumanTakeover(),))
        return await self._voice(ctx)

    async def _voice(self, ctx: TurnContext) -> HandlerResult:
        target = resolve_transfer_target(ctx)
        if target is None:
            reason = "missing" if effective_transfer_number(ctx.node.data) is None else "invalid"
            _log.warning("engine.transfer_target_invalid", node_id=ctx.node.id, reason=reason)
            error = ctx.render(ctx.graph.global_config.error_phrase, field_name="errorPhrase").strip()
            actions: tuple[Action, ...] = (Say(text=error, mark=ctx.next_mark()),) if error else ()
            return HandlerResult(actions, problem=TARGET_INVALID)
        config = ctx.node.data.transfer
        line = await final_line(
            ctx,
            speech=config.speech if config else None,
            speech_mode=config.speech_mode if config else "static",
            speech_prompt=config.speech_prompt if config else None,
        )
        speech, mark = line_actions(ctx, line)
        mode = config.mode if config else "cold"
        transfer = TransferAction(
            mode=mode,
            target=target,
            caller_id_mode=config.caller_id_mode if config else "agent_number",
            warm=config.warm if config is not None and mode == "warm" else None,
            after_mark=mark,
        )
        return HandlerResult((*speech, transfer))

    def waits_for_user(self, node: PathwayNode) -> bool:
        """Never: the AI's part ends with the transfer."""
        del node
        return False
