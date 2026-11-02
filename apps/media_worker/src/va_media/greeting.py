"""The greeting gate run before the opener (specs/07-media-pipeline.md §11 steps 1 and 3; Decision P05-T10b).

:class:`GreetingGate` is the pipeline's ``before_opener`` hook. On outbound calls it

1. waits for the answer (:class:`~va_media.carrier_watch.CarrierWatch`) up to :data:`ANSWER_WAIT_S` on the
   injected clock — Telnyx starts the stream only after the answer, so the wait is short; on timeout it goes
   on (``media.answer_unconfirmed``);
2. sends ``precall_dtmf_sequence`` as digit groups (``command_id("precall_dtmf", call_id, "primary", i)``)
   with the ``w`` pauses, then waits :data:`PRECALL_SETTLE_S`. A Telnyx failure, or no Telnyx client for the
   call, is logged and the gate goes on: the agent still speaks.

Inbound and web calls skip both steps. Waiting for the callee's greeting (step 4) is the pipeline's job.
"""

from __future__ import annotations

from typing import Final

from va_common.logging import get_logger
from va_common.time import Clock
from va_domain.call_context import CallContext
from va_media.carrier_watch import CarrierWatch
from va_media.dtmf_out import DtmfSender, dtmf_steps, send_dtmf_steps
from va_telnyx.client_state import command_id
from va_telnyx.errors import TelnyxError
from va_voice.pipeline.waits import wait_until_set

__all__ = ["ANSWER_WAIT_S", "PRECALL_SETTLE_S", "GreetingGate"]

ANSWER_WAIT_S: Final = 2.0
"""Longest wait for the carrier's answer before the gate goes on anyway."""

PRECALL_SETTLE_S: Final = 0.5
"""Pause after the last precall DTMF group before the agent speaks (§11 step 3)."""

_log = get_logger(__name__)


class GreetingGate:
    """The ``before_opener`` hook of one call; await it once."""

    def __init__(
        self,
        *,
        context: CallContext,
        call_control_id: str,
        watch: CarrierWatch,
        telnyx: DtmfSender | None,
        clock: Clock,
    ) -> None:
        self._context = context
        self._call_control_id = call_control_id
        self._watch = watch
        self._telnyx = telnyx
        self._clock = clock

    async def __call__(self) -> None:
        if self._context.direction != "outbound":
            return
        if not await wait_until_set(self._watch.answered, self._clock, ANSWER_WAIT_S):
            _log.warning("media.answer_unconfirmed", wait_s=ANSWER_WAIT_S)
        sequence = self._context.config.precall_dtmf_sequence
        if sequence:
            await self._send_precall(sequence)

    async def _send_precall(self, sequence: str) -> None:
        if self._telnyx is None:
            _log.warning("media.precall_dtmf_skipped", detail="telnyx_unavailable")
            return
        call_id = self._context.call_id
        try:
            groups = await send_dtmf_steps(
                self._telnyx,
                self._call_control_id,
                dtmf_steps(sequence),
                clock=self._clock,
                command_id_for=lambda i: command_id("precall_dtmf", call_id, "primary", i),
            )
        except (TelnyxError, ValueError) as exc:
            # Never log the digits: they may be an extension or a PIN.
            _log.warning("media.precall_dtmf_failed", error_type=type(exc).__name__)
            return
        _log.info("media.precall_dtmf_sent", groups=groups)
        await self._clock.sleep(PRECALL_SETTLE_S)
