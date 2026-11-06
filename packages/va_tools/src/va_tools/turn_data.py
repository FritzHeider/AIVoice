"""Per-turn dynamic data: the ``timing: "every_turn"`` fetches of one call (specs/13 §7 Decisions P08-T03b
and P08-FIX-23).

:class:`TurnDynamicData` is the :class:`va_engine.turn_data.TurnData` of a call over its
:class:`~va_tools.dynamic_data.DynamicDataFetcher`. It keeps the context lines of each per-turn fetch
(starting from the lines the pre-call phase stored in ``CallContext.turn_sections``) and, on
:meth:`~TurnDynamicData.refresh`, fetches every entry that is not paused again, concurrently and in one
scope, each request with :data:`TURN_TIMEOUT_MS` and the whole refresh within :data:`TURN_BUDGET_S` of the
fetcher's clock.

A refresh changes nothing: it returns a :class:`FetchedTurn` holding the fetches and the variables of those
that count. Only :meth:`FetchedTurn.apply`, which the turn calls in the same synchronous step in which it
merges the variables (:func:`va_engine.turn_data.commit_refresh`), puts the lines in effect, counts the
fetches towards a pause and reports them, so lines, events, variables and pauses change together or (a turn
cut before that step) not at all:

* a fetch that succeeded with a readable answer (a whole JSON document) counts: it replaces its lines
  wholesale (a variable its answer no longer has loses its line and keeps its value) and contributes its
  variables, a later entry winning a name;
* a failed or timed-out fetch changes nothing: the earlier lines and values stay (the stale value). So does
  a 2xx answer that is not readable (an empty body, a body that is not JSON, one cut at the size cap): it
  says nothing about the data, and a proxy's 200 error page must not erase what a 500 would have kept;
* an entry that does not count also holds the names its last counted fetch of this call set: an EARLIER
  entry that sets one of them this turn does not get it (two entries that share a name must not flip to
  the earlier one's value because the later one failed once). An entry that has not counted yet in this
  call holds nothing (the pre-call lines say nothing about names);
* an entry whose endpoint does not answer is paused. A fetch is *unanswered* when its outcome is ``timeout``
  or ``connection`` (no response arrived). After :data:`PAUSE_AFTER` consecutive unanswered fetches that
  were applied, the entry is not fetched for :data:`PAUSE_S` seconds of the fetcher's clock, counted from
  that step: no request, no cache lookup, no wait (a refresh whose entries are all paused awaits nothing).
  The first refresh at or after the end of the pause fetches it again; unanswered, it is paused again at
  once and twice as long, up to :data:`MAX_PAUSE_S`; any other outcome (an answer of any status, readable
  or not, cached or not; a request that was refused or could not be built) ends the streak and the next
  pause starts at :data:`PAUSE_S` again. A paused entry is one that does not count (its lines, its value
  and the names it holds stay) and is reported with the outcome ``paused``. Fetches of a refresh that is
  never applied count nothing, as they report nothing. The pause is this call's alone;
* every fetch is reported to the call's :class:`DynamicDataEvents` (``call.dynamic_data``), in
  ``dynamic_data`` order; a reporter that raises is logged (``tools.turn_data_event_failed``) and ignored.

The media worker reads a per-turn answer up to :data:`TURN_MAX_ANSWER_BYTES` (a larger one is cut, so it is
not readable) and takes a longer stored answer for a miss: the answer is parsed and searched on the event
loop that paces the audio of every call of the pod.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from pydantic import JsonValue

from va_common.logging import get_logger
from va_domain.calls.tools import DynamicDataSpec
from va_engine.templating import TemplateScope
from va_tools.dynamic_data import DynamicDataFetcher, DynamicDataResult

__all__ = [
    "MAX_PAUSE_S",
    "PAUSE_AFTER",
    "PAUSE_S",
    "TURN_BUDGET_S",
    "TURN_MAX_ANSWER_BYTES",
    "TURN_TIMEOUT_MS",
    "DynamicDataEvents",
    "FetchedTurn",
    "TurnDynamicData",
    "turn_specs",
]

TURN_TIMEOUT_MS: Final = 800
"""Each per-turn request; per-turn requests are not retried."""
TURN_BUDGET_S: Final = 0.8
"""The whole refresh of a turn, on the fetcher's clock."""
TURN_MAX_ANSWER_BYTES: Final = 65_536
"""Bytes of a per-turn answer the media worker reads, and of a stored answer body it hits."""
PAUSE_AFTER: Final = 2
"""Consecutive applied unanswered fetches after which an entry is paused."""
PAUSE_S: Final = 30.0
"""The first pause of an entry, on the fetcher's clock; each consecutive pause is twice as long."""
MAX_PAUSE_S: Final = 120.0
"""The longest pause."""

_UNANSWERED: Final = frozenset({"timeout", "connection"})
"""The outcomes of a fetch no response arrived for."""

_log = get_logger(__name__)


class DynamicDataEvents(Protocol):
    """Where the fetches of a refresh are reported (the call's recorder)."""

    def dynamic_data(self, result: DynamicDataResult) -> None: ...


@dataclass(slots=True)
class _Health:
    """How an entry's endpoint has been answering: the applied unanswered fetches in a row, the length of
    the last pause (0 before the first) and the monotonic time the current pause ends."""

    unanswered: int = 0
    pause_s: float = 0.0
    paused_until: float | None = None


def turn_specs(dynamic_data: Sequence[DynamicDataSpec]) -> list[tuple[int, DynamicDataSpec]]:
    """The ``timing: "every_turn"`` entries of ``dynamic_data`` with their positions in it."""
    return [(index, spec) for index, spec in enumerate(dynamic_data) if spec.timing == "every_turn"]


class TurnDynamicData:
    """The per-turn fetches of one call (see the module docstring).

    ``specs`` are the :func:`turn_specs` of the call; ``initial`` are the lines each had when the call
    started (by ``dynamic_data`` index; other indexes are ignored).
    """

    def __init__(
        self,
        fetcher: DynamicDataFetcher,
        specs: Sequence[tuple[int, DynamicDataSpec]],
        *,
        initial: Mapping[int, Sequence[str]] | None = None,
        events: DynamicDataEvents | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._specs = sorted(specs, key=lambda item: item[0])
        self._events = events
        stored = {} if initial is None else initial
        self._lines: dict[int, tuple[str, ...]] = {
            index: tuple(stored.get(index, ())) for index, _ in self._specs
        }
        self._names: dict[int, frozenset[str]] = {}
        """The variable names each entry's last applied counted fetch set."""
        self._health: dict[int, _Health] = {}
        """The entries whose last applied fetch was unanswered (an entry without a key is healthy)."""

    def sections(self) -> tuple[str, ...]:
        """The lines of every per-turn fetch as of the last applied refresh in which it counted, the
        fetches in ``dynamic_data`` order."""
        return tuple(line for index, _ in self._specs for line in self._lines[index])

    async def refresh(self, scope: TemplateScope) -> FetchedTurn:
        """Fetch every entry that is not paused again in ``scope``. No line changes, no fetch counts
        towards a pause and nothing is reported until the result is applied."""
        now = self._fetcher.clock.monotonic()
        paused = {index for index, _ in self._specs if self._paused(index, now)}
        fetched = iter(
            await self._fetcher.fetch_all(
                [entry for entry in self._specs if entry[0] not in paused],
                scope,
                timeout_ms=TURN_TIMEOUT_MS,
                budget_s=TURN_BUDGET_S,
            )
        )
        results = [
            self._fetcher.unfetched(spec, index, scope, "paused") if index in paused else next(fetched)
            for index, spec in self._specs
        ]
        return FetchedTurn(variables=self._turn_variables(results), results=tuple(results), owner=self)

    def commit(self, results: Sequence[DynamicDataResult]) -> None:
        """Put the lines of ``results`` in effect, count them towards a pause and report them
        (:meth:`FetchedTurn.apply`)."""
        now = self._fetcher.clock.monotonic()
        for result in results:
            self._observe(result, now)
            if result.readable:
                self._lines[result.index] = tuple(result.sections.values())
                self._names[result.index] = frozenset(result.variables)
        self._report(results)

    def _paused(self, index: int, now: float) -> bool:
        health = self._health.get(index)
        return health is not None and health.paused_until is not None and now < health.paused_until

    def _observe(self, result: DynamicDataResult, now: float) -> None:
        """Count an applied fetch: an unanswered one extends the entry's streak and, from the
        :data:`PAUSE_AFTER`-th on, pauses it; a paused one changes nothing; any other ends the streak."""
        if result.outcome == "paused":
            return
        if result.outcome not in _UNANSWERED:
            self._health.pop(result.index, None)
            return
        health = self._health.setdefault(result.index, _Health())
        health.unanswered += 1
        if health.unanswered < PAUSE_AFTER:
            return
        health.pause_s = PAUSE_S if health.pause_s <= 0 else min(health.pause_s * 2, MAX_PAUSE_S)
        health.paused_until = now + health.pause_s
        _log.warning(
            "tools.turn_data_paused",
            index=result.index,
            pause_s=health.pause_s,
            unanswered=health.unanswered,
        )

    def _turn_variables(self, results: Sequence[DynamicDataResult]) -> dict[str, JsonValue]:
        """The variables of a turn's fetches, taken in ``dynamic_data`` order: a fetch that counts sets its
        variables; one that does not takes back the names it holds from the entries before it."""
        variables: dict[str, JsonValue] = {}
        for result in results:
            if result.readable:
                variables.update(result.variables)
            else:
                for name in self._names.get(result.index, ()):
                    variables.pop(name, None)
        return variables

    def _report(self, results: Sequence[DynamicDataResult]) -> None:
        events = self._events
        if events is None:
            return
        try:
            for result in results:
                events.dynamic_data(result)
        except Exception as exc:  # reporting never costs the turn its refreshed values
            _log.warning("tools.turn_data_event_failed", error_type=type(exc).__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class FetchedTurn:
    """What one refresh fetched, not in effect yet (a :class:`va_engine.turn_data.TurnRefresh`).

    ``variables`` are the variables of the fetches that count (a readable answer), a later entry winning a
    name and an entry that does not count keeping the names it holds from the earlier ones; ``results``
    every entry in ``dynamic_data`` order, a paused one as a ``paused`` result that was not fetched.
    """

    variables: Mapping[str, JsonValue]
    results: tuple[DynamicDataResult, ...]
    owner: TurnDynamicData

    def apply(self) -> None:
        """Replace the lines of the fetches that count, count every fetch towards a pause and report it;
        synchronous."""
        self.owner.commit(self.results)
