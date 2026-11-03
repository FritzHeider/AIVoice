"""Built-in template variables of a call (specs/11-pathways-engine.md §8, Decision P05-T09a).

``now_utc``/``now_local`` are ISO 8601 with seconds precision (``now_local`` in the call's ``timezone``),
``today`` is the local date. ``agent_number`` is our side of the call (``from_number`` for outbound and web
calls, ``to_number`` for inbound) and ``user_number`` the other side. ``voice_id`` is the platform voice id
as a string.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from va_domain.call_context import CallContext
from va_engine.templating import zone_named

__all__ = ["builtin_values", "call_builtins", "call_zone"]


def call_zone(context: CallContext) -> ZoneInfo:
    """The call's ``timezone`` as a zone (UTC when the name is unknown)."""
    return zone_named(context.config.timezone)


def builtin_values(  # noqa: PLR0913 - one keyword per built-in variable source
    *,
    now: datetime,
    timezone: str,
    from_number: str,
    to_number: str,
    call_id: str,
    voice_id: str | None,
    language: str,
    inbound: bool,
    batch_id: str | None = None,
    metadata: Mapping[str, object] | None = None,
    last_user_message: str = "",
    prev_node_prompt: str = "",
) -> dict[str, object]:
    """The built-in variables of a conversation at ``now`` (an aware datetime) in the zone ``timezone``
    (UTC when unknown): the call's and the pathway test chat's (specs/12 §7 Decision P07-T10)."""
    local = now.astimezone(zone_named(timezone)).replace(microsecond=0)
    return {
        "lastUserMessage": last_user_message,
        "prevNodePrompt": prev_node_prompt,
        "now_utc": now.astimezone(UTC).replace(microsecond=0).isoformat(),
        "now_local": local.isoformat(),
        "today": local.date().isoformat(),
        "from": from_number,
        "to": to_number,
        "call_id": call_id,
        "voice_id": voice_id,
        "timezone": timezone,
        "language": language,
        "agent_number": to_number if inbound else from_number,
        "user_number": from_number if inbound else to_number,
        "batch_id": batch_id,
        "metadata": dict(metadata or {}),
    }


def call_builtins(
    context: CallContext,
    *,
    now: datetime,
    last_user_message: str = "",
    prev_node_prompt: str = "",
) -> dict[str, object]:
    """The built-in variables for ``context`` at ``now`` (an aware datetime)."""
    config = context.config
    return builtin_values(
        now=now,
        timezone=config.timezone,
        from_number=context.from_number,
        to_number=context.to_number,
        call_id=str(context.call_id),
        voice_id=str(config.voice_id),
        language=config.language,
        inbound=context.direction == "inbound",
        batch_id=None if context.batch_id is None else str(context.batch_id),
        metadata=config.metadata,
        last_user_message=last_user_message,
        prev_node_prompt=prev_node_prompt,
    )
