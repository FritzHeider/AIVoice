"""Deterministic ``pathway_logs`` (specs/11 §12; Decision P07-T08c under specs/11 §12).

Pure: both the pathway engine (live test chat, simulations) and the Call Object projection (``va_db``) build
the entries here from the same two inputs, so their output is identical by construction:

- :class:`LogTurn` - one conversation turn record (user or assistant) with its turn index and node;
- :class:`LogEvent` - one engine event (``call.route``, ``call.loop_condition``, ``call.extraction``,
  ``call.webhook_node``, ``call.tool``) in emission order, carrying its ``turn_index`` in the data.

:func:`build_pathway_logs` returns one :class:`~va_domain.call_object.models.PathwayLog` per turn index
that has an assistant turn spoken at a node, ordered by turn index; :func:`pathway_decision` builds the
§12 ``decision`` object of one turn from its events.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from va_domain.call_object.models import PathwayLog, PathwayTag

__all__ = [
    "CONDITION_EVENT",
    "EXTRACTION_EVENT",
    "LOG_EVENT_TYPES",
    "ROUTE_EVENT",
    "TOOL_EVENTS",
    "LogEvent",
    "LogTurn",
    "build_pathway_logs",
    "event_turn_index",
    "pathway_decision",
]

ROUTE_EVENT: Final = "call.route"
CONDITION_EVENT: Final = "call.loop_condition"
EXTRACTION_EVENT: Final = "call.extraction"
TOOL_EVENTS: Final = ("call.webhook_node", "call.tool")
LOG_EVENT_TYPES: Final = (ROUTE_EVENT, CONDITION_EVENT, EXTRACTION_EVENT, *TOOL_EVENTS)
"""The event types a pathway log entry is built from."""


@dataclass(frozen=True, slots=True, kw_only=True)
class LogTurn:
    """One recorded turn (``role`` ``user`` or ``assistant``; other roles are ignored)."""

    turn_index: int
    role: str
    text: str
    node_id: str | None
    node_name: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class LogEvent:
    """One engine event of the call, in emission order."""

    event_type: str
    data: Mapping[str, Any]


def event_turn_index(data: Mapping[str, Any]) -> int | None:
    """The event's ``turn_index`` (``None`` when absent or not an integer; booleans are not integers)."""
    value = data.get("turn_index")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _route(data: Mapping[str, Any]) -> dict[str, Any]:
    alternatives = data.get("alternatives")
    return {
        "from": data.get("from_node"),
        "to": data.get("to_node"),
        "label": data.get("label"),
        "alternatives": list(alternatives) if isinstance(alternatives, list) else [],
    }


def _tool(data: Mapping[str, Any]) -> dict[str, Any]:
    name = _text(data.get("tool_name")) or _text(data.get("node_name")) or _text(data.get("node_id"))
    return {"name": name, "status": data.get("status_code"), "ms": data.get("duration_ms")}


def _last(events: Sequence[LogEvent], event_type: str) -> Mapping[str, Any] | None:
    found = [event.data for event in events if event.event_type == event_type]
    return found[-1] if found else None


def _extracted(events: Sequence[LogEvent]) -> dict[str, Any] | None:
    merged: dict[str, Any] | None = None
    for event in events:
        if event.event_type == EXTRACTION_EVENT:
            variables = event.data.get("variables")
            merged = {**(merged or {}), **(variables if isinstance(variables, Mapping) else {})}
    return merged


def pathway_decision(events: Sequence[LogEvent]) -> dict[str, Any]:
    """The §12 ``decision`` of one turn from its events (emission order; later events win)."""
    route = _last(events, ROUTE_EVENT)
    condition = _last(events, CONDITION_EVENT)
    return {
        "route": None if route is None else _route(route),
        "global": route is not None and route.get("global") is True,
        "loop_condition": None
        if condition is None
        else {"score": condition.get("score"), "met": condition.get("met")},
        "extracted": _extracted(events),
        "tools": [_tool(event.data) for event in events if event.event_type in TOOL_EVENTS],
    }


def _assistant_turns(turns: Sequence[LogTurn]) -> dict[int, list[LogTurn]]:
    found: dict[int, list[LogTurn]] = defaultdict(list)
    for turn in turns:
        if turn.role == "assistant" and turn.node_id is not None:
            found[turn.turn_index].append(turn)
    return found


def _user_texts(turns: Sequence[LogTurn]) -> dict[int, str]:
    return {turn.turn_index: turn.text for turn in turns if turn.role == "user"}


def _events_by_turn(events: Sequence[LogEvent]) -> dict[int, list[LogEvent]]:
    found: dict[int, list[LogEvent]] = defaultdict(list)
    for event in events:
        index = event_turn_index(event.data)
        if index is not None and event.event_type in LOG_EVENT_TYPES:
            found[index].append(event)
    return found


def _route_color(events: Sequence[LogEvent]) -> tuple[bool, str | None]:
    """Whether the turn has a route, and the last route's ``tag_color``."""
    routes = [event for event in events if event.event_type == ROUTE_EVENT]
    if not routes:
        return False, None
    return True, _text(routes[-1].data.get("tag_color"))


def build_pathway_logs(
    turns: Sequence[LogTurn],
    events: Sequence[LogEvent],
    *,
    pathway_id: uuid.UUID | None,
    pathway_version: int | None,
) -> list[PathwayLog]:
    """One entry per turn index with an assistant turn spoken at a node, ordered by turn index.

    ``text`` joins the turn's stripped, non-blank assistant texts with one space; ``node_id`` and
    ``tag.name`` are the last assistant turn's; ``created_at`` the first one's. ``tag.color`` is the colour of
    the turn's last route, carried over from the latest earlier route when the turn has none.
    """
    assistants = _assistant_turns(turns)
    users = _user_texts(turns)
    by_turn = _events_by_turn(events)
    logs: list[PathwayLog] = []
    color: str | None = None
    for index in sorted(assistants.keys() | by_turn.keys()):
        turn_events = by_turn.get(index, [])
        routed, route_color = _route_color(turn_events)
        if routed:
            color = route_color
        spoken = assistants.get(index)
        if not spoken:
            continue
        last = spoken[-1]
        logs.append(
            PathwayLog(
                tag=PathwayTag(name=last.node_name, color=color),
                node_id=last.node_id or "",
                pathway_id=pathway_id,
                pathway_version=pathway_version,
                role="assistant",
                text=" ".join(part for part in (turn.text.strip() for turn in spoken) if part),
                user_text=users.get(index),
                decision=pathway_decision(turn_events),
                created_at=spoken[0].created_at,
            )
        )
    return logs
