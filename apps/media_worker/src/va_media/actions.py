"""The pipeline's action handler on the media worker (specs/07-media-pipeline.md §16; Decision P05-T10b).

:class:`MediaActions` executes the telephony outputs of the Brain:

- ``PressButton``: the digits go out as Telnyx ``send_dtmf`` groups with their ``w``/``W`` pauses
  (``va_media.dtmf_out``), ``command_id("press_button", call_id, "primary", n, i)`` (``n`` counts the
  presses of the call, ``i`` the groups of one press), so a retried command is deduplicated and the next press
  is not. A Telnyx failure is logged and swallowed: an action failure would end the call.
- every other action (``transfer_call`` until P05-T13) is logged ``media.action_unsupported``.

The digits are never logged (they may be a PIN or an account number).
"""

from __future__ import annotations

import uuid

from va_common.logging import get_logger
from va_common.time import Clock
from va_engine.models.results import PressButton
from va_media.dtmf_out import DtmfSender, dtmf_steps, send_dtmf_steps
from va_telnyx.client_state import command_id
from va_telnyx.errors import TelnyxError
from va_voice.pipeline import BrainAction

__all__ = ["MediaActions"]

_log = get_logger(__name__)


class MediaActions:
    """The :class:`~va_voice.pipeline.ActionHandler` of one call."""

    def __init__(
        self, *, call_id: uuid.UUID, call_control_id: str, telnyx: DtmfSender | None, clock: Clock
    ) -> None:
        self._call_id = call_id
        self._call_control_id = call_control_id
        self._telnyx = telnyx
        self._clock = clock
        self._presses = 0

    @property
    def presses(self) -> int:
        """``press_button`` actions handled so far (sent or failed)."""
        return self._presses

    async def handle(self, action: BrainAction) -> None:
        if isinstance(action, PressButton):
            await self._press(action.digits)
            return
        _log.info("media.action_unsupported", kind=action.kind)

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
