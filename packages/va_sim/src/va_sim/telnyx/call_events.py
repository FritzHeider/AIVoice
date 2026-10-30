"""Ordered call webhooks and per-call timers for the FakeTelnyx call engine (specs/30-testing-and-evals.md
§3.2).

State changes happen synchronously in the request handler or timer that causes them; their webhooks are
delivered by supervised background tasks after the response. Deliveries are chained per call session (a leg
plus its transfer and ``link_to`` legs) and per conference: each delivery task waits for the previous one of
every chain it touches, so events arrive in the order the engine produced them, also across the two legs of a
bridge and across the participants of a conference.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from va_sim.clock import iso_z
from va_sim.telnyx.calls import Call, EventEntry
from va_sim.telnyx.store import Record

if TYPE_CHECKING:
    from va_sim.state import SimState

__all__ = ["CallEvents", "Outgoing"]


@dataclass(frozen=True, slots=True)
class Outgoing:
    """One webhook for ``call``; ``prepare`` runs right before it is sent (it stores a recording file);
    ``conference_id`` also orders it within that conference's events."""

    call: Call
    url: str
    event_type: str
    payload: Record
    prepare: Callable[[], Awaitable[None]] | None = None
    conference_id: str | None = None

    @property
    def chains(self) -> set[str]:
        keys = {f"session:{self.call.call_session_id}"}
        if self.conference_id is not None:
            keys.add(f"conference:{self.conference_id}")
        return keys


class CallEvents:
    """Webhook ordering and timers shared by the call engine."""

    def __init__(self, state: SimState) -> None:
        self._state = state
        self._tails: dict[str, asyncio.Task[None]] = {}

    def event(
        self,
        call: Call,
        event_type: str,
        payload: Record | None = None,
        prepare: Callable[[], Awaitable[None]] | None = None,
        conference_id: str | None = None,
    ) -> Outgoing:
        """A webhook for ``call`` with the payload as it is now (the common call payload by default)."""
        body = call.payload() if payload is None else payload
        return Outgoing(call, call.webhook_url, event_type, body, prepare, conference_id)

    def emit(self, call: Call, event_type: str, **extra: object) -> None:
        """Send ``event_type`` for ``call`` with the common payload plus ``extra``."""
        self.send([self.event(call, event_type, call.payload(**extra))])

    def send(self, items: Iterable[Outgoing]) -> None:
        """Deliver ``items`` in order, after every delivery already scheduled on their chains."""
        batch = tuple(items)
        if not batch:
            return
        chains = set().union(*(item.chains for item in batch))
        previous = tuple({self._tails[chain] for chain in chains if chain in self._tails})
        task = self._state.tasks.spawn(
            self._deliver(previous, batch), name=f"telnyx-call-events-{batch[0].call.call_control_id}"
        )
        for chain in chains:
            self._tails[chain] = task
        task.add_done_callback(functools.partial(self._forget, chains))

    def _forget(self, chains: set[str], task: asyncio.Task[None]) -> None:
        for chain in chains:
            if self._tails.get(chain) is task:
                del self._tails[chain]

    async def _deliver(self, previous: tuple[asyncio.Task[None], ...], batch: tuple[Outgoing, ...]) -> None:
        if previous:
            await asyncio.wait(previous)
        for item in batch:
            if item.prepare is not None:
                await item.prepare()
            event_id = await self._state.webhooks.emit(item.url, item.event_type, item.payload)
            item.call.events.append(EventEntry(item.event_type, event_id, iso_z(self._state.clock.now())))

    def later(self, call: Call, delay_s: float, action: Callable[[], None], *, name: str) -> None:
        """Run ``action`` after ``delay_s`` on the sim clock unless the call's timers are cancelled first."""

        async def run() -> None:
            await self._state.clock.sleep(delay_s)
            action()

        task = self._state.tasks.spawn(run(), name=f"telnyx-call-{name}-{call.call_control_id}")
        call.timers.add(task)
        task.add_done_callback(call.timers.discard)

    @staticmethod
    def cancel_timers(call: Call) -> None:
        """Cancel the call's pending timers (never the task running this code)."""
        current = asyncio.current_task()
        for task in tuple(call.timers):
            if task is not current:
                task.cancel()

    async def idle(self) -> None:
        """Wait until every webhook scheduled so far has been delivered (first attempts)."""
        while self._tails:
            await asyncio.wait(tuple(self._tails.values()))
            await asyncio.sleep(0)

    def reset(self) -> None:
        """Forget delivery chains (``/sim/reset`` cancels the tasks themselves)."""
        self._tails.clear()
