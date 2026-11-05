"""Callback rules of the ``schedule_callback`` system tool (specs/13 §6; Decision P08-T02b2).

A callback is a scheduled outbound call to the other party of the current call at the time the model asked
for. :func:`callback_time` applies the Send Call ``start_time`` rules (specs/05 §3.3): an offset is kept, a
naive time is localized in the call's ``timezone``, and the result must lie in the future and at most
:data:`~va_domain.call_resolution.MAX_SCHEDULE_AHEAD` ahead. :func:`callback_config` builds the child's
``calls.config`` from the parent's.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final, Literal
from zoneinfo import ZoneInfo

from pydantic import JsonValue

from va_domain.call_resolution.resolve import MAX_SCHEDULE_AHEAD
from va_domain.call_retry import RETRY_ATTEMPT_KEY, iso_utc
from va_domain.calls.parts import parse_start_time
from va_domain.numbers import MAINTENANCE_MESSAGE_KEY

__all__ = [
    "CALLBACK_NOTE_KEY",
    "CallbackTimeError",
    "CallbackTimeReason",
    "callback_config",
    "callback_time",
]

CALLBACK_NOTE_KEY: Final = "callback_note"
"""The ``calls.config`` key holding the model's note for a callback child (a non-configuration key)."""

type CallbackTimeReason = Literal["invalid_datetime", "in_the_past", "too_far_ahead"]


class CallbackTimeError(ValueError):
    """The requested callback time is unusable; ``reason`` is the machine reason the model is told."""

    def __init__(self, reason: CallbackTimeReason) -> None:
        super().__init__(reason)
        self.reason: CallbackTimeReason = reason


def callback_time(value: str, timezone: str, now: datetime) -> datetime:
    """The UTC moment of ``value`` (naive → ``timezone``); :class:`CallbackTimeError` when it cannot be
    parsed, is not after ``now``, or lies more than 90 days ahead."""
    try:
        parsed = parse_start_time(value)
    except ValueError as exc:
        raise CallbackTimeError("invalid_datetime") from exc
    if not isinstance(parsed, datetime):  # pragma: no cover - parse_start_time returns datetimes for str
        raise CallbackTimeError("invalid_datetime")
    aware = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=ZoneInfo(timezone), fold=0)
    moment = aware.astimezone(UTC)
    if moment <= now:
        raise CallbackTimeError("in_the_past")
    if moment > now + MAX_SCHEDULE_AHEAD:
        raise CallbackTimeError("too_far_ahead")
    return moment


def callback_config(
    stored_config: Mapping[str, JsonValue], scheduled_for: datetime, note: str | None
) -> dict[str, JsonValue]:
    """The child's ``calls.config``: a copy of the parent's with ``start_time`` at ``scheduled_for``, without
    the parent's retry attempt and maintenance message, and with ``callback_note`` when ``note`` is not
    blank. ``stored_config`` is not modified."""
    config: dict[str, JsonValue] = dict(stored_config)
    config.pop(RETRY_ATTEMPT_KEY, None)
    config.pop(MAINTENANCE_MESSAGE_KEY, None)
    config.pop(CALLBACK_NOTE_KEY, None)
    config["start_time"] = iso_utc(scheduled_for)
    if note is not None and note.strip():
        config[CALLBACK_NOTE_KEY] = note.strip()
    return config
