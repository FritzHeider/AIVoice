"""Per-turn data: the port through which a conversation refreshes its ``timing: "every_turn"`` dynamic data
(specs/13 §7 Decision P08-T03b).

A :class:`TurnData` holds the context lines of the conversation's per-turn fetches (the tail of the prompt's
``## Context`` block) and re-fetches them when a caller turn starts. A refresh changes nothing by itself: it
returns a :class:`TurnRefresh`, the fetched variables plus an ``apply()`` that puts the fetched lines in
effect and reports the fetches. The runners await it through :func:`refresh_turn`, which never raises (a
failing port is logged ``engine.turn_data_failed`` and yields nothing; cancellation passes through), and put
it in effect with :func:`commit_refresh`: ``apply()`` and the merge of the variables into the state are ONE
synchronous step, taken only once everything the turn awaits beside the refresh has finished. A turn cut
before that step, while the refresh runs or after it finished, leaves no trace: no variable, no line, no
event.

The implementation over the dynamic data fetcher is :class:`va_tools.turn_data.TurnDynamicData`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from pydantic import JsonValue

from va_common.logging import get_logger
from va_engine.templating import TemplateScope

__all__ = ["TurnData", "TurnRefresh", "commit_refresh", "merge_refreshed", "refresh_turn"]

_log = get_logger(__name__)


class TurnRefresh(Protocol):
    """What one refresh fetched, not in effect yet."""

    @property
    def variables(self) -> Mapping[str, JsonValue]:
        """The variables of the fetches that succeeded."""
        ...

    def apply(self) -> None:
        """Put the fetched lines in effect (:meth:`TurnData.sections` then reflects them) and report the
        fetches. Synchronous; called at most once, by the turn that asked for the refresh."""
        ...


class TurnData(Protocol):
    """The per-turn fetches of one conversation."""

    def sections(self) -> tuple[str, ...]:
        """The context lines of the per-turn fetches as of the last refresh that was applied."""
        ...

    async def refresh(self, scope: TemplateScope) -> TurnRefresh:
        """Fetch again in ``scope``. Changes no line and reports nothing: that is the result's
        :meth:`~TurnRefresh.apply`."""
        ...


async def refresh_turn(port: TurnData | None, scope: TemplateScope) -> TurnRefresh | None:
    """What ``port`` fetched in ``scope``, to be committed: ``None`` without a port or when it raises."""
    if port is None:
        return None
    try:
        return await port.refresh(scope)
    except Exception as exc:  # per-turn data never fails a turn: the reply goes on with the earlier values
        _log.warning("engine.turn_data_failed", error_type=type(exc).__name__)
        return None


def commit_refresh(variables: dict[str, JsonValue], refresh: TurnRefresh | None) -> bool:
    """Put ``refresh`` in effect: apply it and set its variables over ``variables`` in place, in one
    synchronous step. Whether a variable was new or its value differed.

    A refresh whose ``apply`` raises is logged (``engine.turn_data_failed``) and merges nothing.
    """
    if refresh is None:
        return False
    try:
        refresh.apply()
    except Exception as exc:  # per-turn data never fails a turn
        _log.warning("engine.turn_data_failed", error_type=type(exc).__name__)
        return False
    return merge_refreshed(variables, refresh.variables)


def merge_refreshed(variables: dict[str, JsonValue], refreshed: Mapping[str, JsonValue]) -> bool:
    """Set ``refreshed`` over ``variables`` in place; whether a name was new or its value differed."""
    changed = False
    for name, value in refreshed.items():
        if name not in variables or variables[name] != value:
            changed = True
        variables[name] = value
    return changed
