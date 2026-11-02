"""Built-in template variables of a call (specs/11-pathways-engine.md §8, Decision P05-T09a).

``now_utc``/``now_local`` are ISO 8601 with seconds precision (``now_local`` in the call's ``timezone``),
``today`` is the local date. ``agent_number`` is our side of the call (``from_number`` for outbound and web
calls, ``to_number`` for inbound) and ``user_number`` the other side. ``voice_id`` is the platform voice id
as a string.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from va_domain.call_context import CallContext
from va_engine.templating import zone_named

__all__ = ["call_builtins", "call_zone"]


def call_zone(context: CallContext) -> ZoneInfo:
    """The call's ``timezone`` as a zone (UTC when the name is unknown)."""
    return zone_named(context.config.timezone)


def call_builtins(
    context: CallContext,
    *,
    now: datetime,
    last_user_message: str = "",
    prev_node_prompt: str = "",
) -> dict[str, object]:
    """The built-in variables for ``context`` at ``now`` (an aware datetime)."""
    config = context.config
    local = now.astimezone(call_zone(context)).replace(microsecond=0)
    inbound = context.direction == "inbound"
    return {
        "lastUserMessage": last_user_message,
        "prevNodePrompt": prev_node_prompt,
        "now_utc": now.astimezone(UTC).replace(microsecond=0).isoformat(),
        "now_local": local.isoformat(),
        "today": local.date().isoformat(),
        "from": context.from_number,
        "to": context.to_number,
        "call_id": str(context.call_id),
        "voice_id": str(config.voice_id),
        "timezone": config.timezone,
        "language": config.language,
        "agent_number": context.to_number if inbound else context.from_number,
        "user_number": context.from_number if inbound else context.to_number,
        "batch_id": None if context.batch_id is None else str(context.batch_id),
        "metadata": dict(config.metadata),
    }
