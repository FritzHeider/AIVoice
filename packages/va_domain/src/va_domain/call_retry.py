"""Voicemail retry rules (specs/05-calls-api.md §3 ``retry``; specs/07 §14 Decision P05-T10f).

When a call that carries ``retry`` ends on voicemail (or on no-answer/busy with ``retry.on_no_answer``), a
child call is created as an ordinary scheduled call ``retry.wait`` seconds later. These pure functions decide
whether a retry is wanted, count the attempts of a chain, and build the child's ``calls.config``.

``max_attempts`` is the number of retry calls in the chain: the original call is attempt 0, each child stores
its attempt number in ``calls.config.retry_attempt`` (a non-configuration key like ``pricing``), and a child
is created only while the ending call's attempt is below ``max_attempts``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final

from pydantic import JsonValue

from va_domain.calls.config import RetryConfig

__all__ = [
    "NO_ANSWER_END_REASONS",
    "RETRY_ATTEMPT_KEY",
    "VOICEMAIL_END_REASONS",
    "child_config",
    "retry_attempt",
    "retry_wanted",
]

VOICEMAIL_END_REASONS: Final = frozenset({"voicemail_hangup", "voicemail_left"})
"""End reasons that always trigger a retry of a call with ``retry``."""
NO_ANSWER_END_REASONS: Final = frozenset({"no_answer", "busy"})
"""End reasons that trigger a retry only with ``retry.on_no_answer``."""
RETRY_ATTEMPT_KEY: Final = "retry_attempt"
"""The ``calls.config`` key holding a retry child's attempt number (absent on the original call)."""


def retry_wanted(retry: RetryConfig | None, end_reason: str | None) -> bool:
    """Whether a call configured with ``retry`` that ended with ``end_reason`` asks for a retry call."""
    if retry is None or end_reason is None:
        return False
    if end_reason in VOICEMAIL_END_REASONS:
        return True
    return retry.on_no_answer and end_reason in NO_ANSWER_END_REASONS


def retry_attempt(stored_config: Mapping[str, object]) -> int:
    """The attempt number stored in ``calls.config`` (0 for the original call or an unusable value)."""
    value = stored_config.get(RETRY_ATTEMPT_KEY)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def child_config(
    stored_config: Mapping[str, JsonValue], retry: RetryConfig, attempt: int, scheduled_for: datetime
) -> dict[str, JsonValue]:
    """The child's ``calls.config``: a copy of the parent's (``retry`` and ``pricing`` kept, so later attempts
    continue the chain), the retry's voicemail action (and message, when it has one), ``start_time`` at
    ``scheduled_for``, and ``retry_attempt = attempt``. ``stored_config`` is not modified."""
    config: dict[str, JsonValue] = dict(stored_config)
    stored_voicemail = config.get("voicemail")
    voicemail: dict[str, JsonValue] = dict(stored_voicemail) if isinstance(stored_voicemail, dict) else {}
    voicemail["action"] = retry.voicemail_action
    if retry.voicemail_message is not None:
        voicemail["message"] = retry.voicemail_message
    config["voicemail"] = voicemail
    config["start_time"] = _iso(scheduled_for)
    config[RETRY_ATTEMPT_KEY] = attempt
    return config
