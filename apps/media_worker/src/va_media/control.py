"""Handling control messages of an attached call (specs/07-media-pipeline.md §17; specs/07 §3 Decision
P05-T07b).

The session's ``control_listener`` hands each message, in arrival order, to a :class:`ControlHandler`; a
``True`` result means "handled" and the session writes the ``control_ack`` key (not for local messages).
:class:`DefaultControlHandler` covers what exists before the pipeline: ``stop`` and ``credits_exhausted`` end
the session, ``drain_notice`` is logged; ``say``, ``inject_context``, ``transfer``, and ``takeover`` need the
pipeline and the transfer flow, so they are logged and left unacknowledged (the API then answers "queued").
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from va_common.logging import get_logger
from va_domain.control import ControlMessage, CreditsExhaustedControl, DrainNoticeControl, StopControl

if TYPE_CHECKING:
    from va_media.session import CallSession

__all__ = ["ControlHandler", "DefaultControlHandler"]

_log = get_logger(__name__)


class ControlHandler(Protocol):
    async def handle(self, session: CallSession, message: ControlMessage) -> bool:
        """Handle ``message``; ``True`` when it was carried out (the session acknowledges it)."""
        ...


class DefaultControlHandler:
    """Stop, credit exhaustion, and the drain notice; everything else is logged as unsupported."""

    async def handle(self, session: CallSession, message: ControlMessage) -> bool:
        if isinstance(message, StopControl):
            _log.info("media.control_stop", reason=message.reason)
            session.request_end("api_stop", "api")
            return True
        if isinstance(message, CreditsExhaustedControl):
            _log.info("media.control_credits_exhausted")
            session.request_end("insufficient_credits", "system")
            return True
        if isinstance(message, DrainNoticeControl):
            _log.info("media.drain_notice", message_id=str(message.id))
            return True
        _log.warning("media.control_unsupported", type=message.type, message_id=str(message.id))
        return False
