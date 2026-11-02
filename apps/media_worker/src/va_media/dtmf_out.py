"""Outbound DTMF: digit groups and pauses (specs/06-telephony-telnyx.md §9; specs/07 §11 Decision P05-T10b).

A sequence such as ``"12w3"`` is sent as contiguous digit groups, one Telnyx ``send_dtmf`` command each, with
the pauses between them slept on the injected clock: each ``w`` is 0.5 s, each ``W`` (``press_button`` only;
``precall_dtmf_sequence`` allows ``w`` alone) 1.0 s. :func:`dtmf_steps` is pure; :func:`send_dtmf_steps` runs
the steps against any object with ``TelnyxClient.send_dtmf``'s signature.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Final, Protocol

from va_common.time import Clock

__all__ = [
    "DTMF_LONG_PAUSE_S",
    "DTMF_PAUSE_S",
    "DtmfSender",
    "DtmfStep",
    "dtmf_steps",
    "send_dtmf_steps",
]

DTMF_PAUSE_S: Final = 0.5
"""``w``: half a second."""

DTMF_LONG_PAUSE_S: Final = 1.0
"""``W``: one second."""

_TOKEN: Final = re.compile(r"[0-9A-D*#]+|[wW]")
_VALID: Final = re.compile(r"[0-9A-D*#wW]+")

type DtmfStep = str | float
"""A digit group (``str``) to send, or a pause in seconds (``float``)."""

_PAUSES: Final[dict[str, DtmfStep]] = {"w": DTMF_PAUSE_S, "W": DTMF_LONG_PAUSE_S}


class DtmfSender(Protocol):
    """``TelnyxClient.send_dtmf`` as the senders need it."""

    async def send_dtmf(
        self, call_control_id: str, digits: str, duration_millis: int = 250, *, command_id: str
    ) -> object: ...


def dtmf_steps(sequence: str) -> tuple[DtmfStep, ...]:
    """The groups and pauses of ``sequence`` in order; ``ValueError`` when it is empty or has another char."""
    if not _VALID.fullmatch(sequence):
        msg = "a DTMF sequence holds only 0-9, A-D, *, #, w and W"
        raise ValueError(msg)
    return tuple(_PAUSES.get(part, part) for part in _TOKEN.findall(sequence))


async def send_dtmf_steps(
    sender: DtmfSender,
    call_control_id: str,
    steps: Sequence[DtmfStep],
    *,
    clock: Clock,
    command_id_for: Callable[[int], str],
) -> int:
    """Send each group (``command_id_for(i)``, ``i`` = group index) and sleep each pause; returns the number
    of groups sent. A send error propagates (the groups before it went out)."""
    group = 0
    for step in steps:
        if isinstance(step, str):
            await sender.send_dtmf(call_control_id, step, command_id=command_id_for(group))
            group += 1
        else:
            await clock.sleep(step)
    return group
