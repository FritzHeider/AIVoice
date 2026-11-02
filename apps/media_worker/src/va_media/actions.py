"""The pipeline's action handler on the media worker (specs/07-media-pipeline.md §16; Decisions P05-T10b and
P05-T13).

:class:`MediaActions` executes the telephony outputs of the Brain:

- ``PressButton``: the digits go out as Telnyx ``send_dtmf`` groups with their ``w``/``W`` pauses
  (``va_media.dtmf_out``), ``command_id("press_button", call_id, "primary", n, i)`` (``n`` counts the
  presses of the call, ``i`` the groups of one press), so a retried command is deduplicated and the next press
  is not. A Telnyx failure is logged and swallowed: an action failure would end the call.
- ``TransferRequest`` (``transfer_call``): the call's :class:`~va_media.transfer.ColdTransfer` runs it;
  ``bridged`` calls ``on_transferred`` (the runtime ends the AI's part of the call), a failure (or no transfer
  configured: no Telnyx client) calls ``on_transfer_failed`` (the runtime speaks the org's
  ``transfer_failed_message`` and the conversation goes on), ``not_active`` (the call already left
  ``in_progress``) does nothing. The callbacks are bound once the pipeline exists (:meth:`MediaActions.bind`);
- every other action is logged ``media.action_unsupported``.

The digits are never logged (they may be a PIN or an account number).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from va_common.logging import get_logger
from va_common.time import Clock
from va_engine.models.results import PressButton, TransferRequest
from va_media.dtmf_out import DtmfSender, dtmf_steps, send_dtmf_steps
from va_media.transfer import ColdTransfer
from va_telnyx.client_state import command_id
from va_telnyx.errors import TelnyxError
from va_voice.pipeline import BrainAction

__all__ = ["MediaActions"]

_log = get_logger(__name__)


class MediaActions:
    """The :class:`~va_voice.pipeline.ActionHandler` of one call."""

    def __init__(
        self,
        *,
        call_id: uuid.UUID,
        call_control_id: str,
        telnyx: DtmfSender | None,
        clock: Clock,
        transfer: ColdTransfer | None = None,
    ) -> None:
        self._call_id = call_id
        self._call_control_id = call_control_id
        self._telnyx = telnyx
        self._clock = clock
        self._transfer = transfer
        self._presses = 0
        self._on_transferred: Callable[[], None] | None = None
        self._on_transfer_failed: Callable[[], None] | None = None

    def bind(self, *, on_transferred: Callable[[], None], on_transfer_failed: Callable[[], None]) -> None:
        """What a transfer's outcome triggers (the pipeline's takeover / transfer-failed line)."""
        self._on_transferred = on_transferred
        self._on_transfer_failed = on_transfer_failed

    @property
    def presses(self) -> int:
        """``press_button`` actions handled so far (sent or failed)."""
        return self._presses

    async def handle(self, action: BrainAction) -> None:
        if isinstance(action, PressButton):
            await self._press(action.digits)
            return
        if isinstance(action, TransferRequest):
            await self._cold_transfer(action.target)
            return
        _log.info("media.action_unsupported", kind=action.kind)

    async def _cold_transfer(self, target: str) -> None:
        if self._transfer is None:
            _log.warning("media.transfer_unavailable")
            outcome = "command_failed"
        else:
            outcome = await self._transfer.run(target)
        if outcome == "not_active":
            return
        callback = self._on_transferred if outcome == "bridged" else self._on_transfer_failed
        if callback is not None:
            callback()

    async def _press(self, digits: str) -> None:
        press = self._presses
        self._presses += 1
        if self._telnyx is None:
            _log.warning("media.press_button_skipped", detail="telnyx_unavailable")
            return
        call_id = self._call_id
        try:
            groups = await send_dtmf_steps(
                self._telnyx,
                self._call_control_id,
                dtmf_steps(digits),
                clock=self._clock,
                command_id_for=lambda i: command_id("press_button", call_id, "primary", press, i),
            )
        except (TelnyxError, ValueError) as exc:
            _log.warning("media.press_button_failed", error_type=type(exc).__name__)
            return
        _log.info("media.press_button_sent", groups=groups)
