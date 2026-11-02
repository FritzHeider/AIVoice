"""Telephony actions a runner hands to the pipeline's action handler (specs/11 §7, Decision P05-T09c).

They subclass :class:`va_voice.pipeline.BrainAction`, so the pipeline passes them through unchanged; the media
worker's action handler executes them (cold transfer, DTMF).
"""

from __future__ import annotations

from dataclasses import dataclass

from va_voice.pipeline.models import BrainAction

__all__ = ["PressButton", "TransferRequest"]


@dataclass(frozen=True, slots=True, kw_only=True)
class TransferRequest(BrainAction):
    """Transfer the caller to ``target`` (E.164); ``label`` is the transfer list key the model chose."""

    kind: str = "transfer_call"
    target: str
    label: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PressButton(BrainAction):
    """Send ``digits`` as DTMF (``w``/``W`` are pauses)."""

    kind: str = "press_button"
    digits: str
