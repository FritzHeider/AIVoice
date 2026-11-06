"""A scripted :class:`va_engine.turn_data.TurnData` (specs/13 §7 Decision P08-T03b)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from pydantic import JsonValue

from va_engine.templating import TemplateScope

__all__ = ["FakeTurnData", "Fetched", "Refresh"]

type Refresh = tuple[Mapping[str, JsonValue], Sequence[str]] | Exception
"""One scripted refresh: the variables it fetches and the lines it leaves once applied, or what it raises."""


@dataclass(slots=True)
class FakeTurnData:
    """Refreshes from ``script`` in order (an exhausted script fetches nothing and keeps the lines).

    Like the real port a refresh changes nothing: the lines change when its :class:`Fetched` is applied,
    and ``applied`` lists the variables of each applied refresh (what the real port reports as events).
    ``scopes`` are the variables each refresh saw (copied when it started); with ``hold`` a refresh waits
    for the event after ``started`` was set, ``cancelled`` counts the refreshes cut while waiting, and
    ``fetched`` is set whenever a refresh returned.
    """

    lines: tuple[str, ...] = ()
    script: list[Refresh] = field(default_factory=list)
    hold: asyncio.Event | None = None
    scopes: list[dict[str, object]] = field(default_factory=list)
    started: asyncio.Event = field(default_factory=asyncio.Event)
    fetched: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: int = 0
    applied: list[dict[str, JsonValue]] = field(default_factory=list)

    def then(self, variables: Mapping[str, JsonValue], *lines: str) -> FakeTurnData:
        self.script.append((variables, lines))
        return self

    def failing(self, error: Exception) -> FakeTurnData:
        self.script.append(error)
        return self

    def sections(self) -> tuple[str, ...]:
        return self.lines

    async def refresh(self, scope: TemplateScope) -> Fetched:
        self.scopes.append(dict(scope.variables))
        self.started.set()
        if self.hold is not None:
            try:
                await self.hold.wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
        step = self.script.pop(0) if self.script else ({}, self.lines)
        if isinstance(step, Exception):
            raise step
        variables, lines = step
        self.fetched.set()
        return Fetched(variables=dict(variables), lines=tuple(lines), owner=self)


@dataclass(frozen=True, slots=True, kw_only=True)
class Fetched:
    """What one scripted refresh fetched, not in effect until applied."""

    variables: Mapping[str, JsonValue]
    lines: tuple[str, ...]
    owner: FakeTurnData

    def apply(self) -> None:
        self.owner.lines = self.lines
        self.owner.applied.append(dict(self.variables))
