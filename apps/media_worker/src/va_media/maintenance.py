"""The maintenance message of an inbound number (specs/16-phone-numbers.md §4; specs/16 §4 Decision
P05-T12d).

The edge accepts a call to a number in maintenance with a message (and no fallback number) and stores that
message on the call (:attr:`~va_domain.call_context.CallContext.maintenance_message`). The media worker then
takes the pipeline over before the opener: the message is spoken at once, verbatim and not interruptible (no
beep or silence cue to wait for), and the call ends ``inbound_rejected`` (``ended_by`` ``system``). The Brain
is never asked, so no LLM request is made.
"""

from __future__ import annotations

from typing import Final

from va_common.logging import get_logger
from va_domain.call_context import CallContext
from va_voice.pipeline import Takeover

__all__ = ["MAINTENANCE_END", "maintenance_takeover"]

MAINTENANCE_END: Final = "inbound_rejected"
"""The end reason of a call that heard the maintenance message (specs/05 §9)."""

_log = get_logger(__name__)


def maintenance_takeover(context: CallContext) -> Takeover | None:
    """The takeover that speaks the call's maintenance message and ends it; ``None`` when the call carries no
    (or only a blank) message. The message itself is never logged."""
    message = context.maintenance_message
    if message is None or not message.strip():
        return None
    _log.info("media.maintenance_takeover", chars=len(message))
    return Takeover(reason=MAINTENANCE_END, text=message, silence_ms=0, max_wait_s=0.0)
