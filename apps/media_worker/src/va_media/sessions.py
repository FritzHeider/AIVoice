"""The sessions running on this pod, for the drain (specs/07-media-pipeline.md §19; specs/07 §3 Decision
P05-T07b).

The drain notifies every live session and, at the drain timeout, ends the remaining ones; it then waits (on
the injected clock) for them to finish their cleanup.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from va_common.time import Clock
from va_voice.pipeline.waits import wait_until_set

if TYPE_CHECKING:
    from va_media.session import CallSession

__all__ = ["LiveSessions"]


class LiveSessions:
    """Sessions between their start and the end of their cleanup."""

    def __init__(self) -> None:
        self._sessions: list[CallSession] = []
        self._idle = asyncio.Event()
        self._idle.set()

    def __len__(self) -> int:
        return len(self._sessions)

    def add(self, session: CallSession) -> None:
        self._sessions.append(session)
        self._idle.clear()

    def remove(self, session: CallSession) -> None:
        if session in self._sessions:
            self._sessions.remove(session)
        if not self._sessions:
            self._idle.set()

    def all(self) -> tuple[CallSession, ...]:
        return tuple(self._sessions)

    async def wait_idle(self, clock: Clock, timeout_s: float) -> bool:
        """Wait until no session runs; ``False`` when ``timeout_s`` of ``clock`` time elapses first."""
        return await wait_until_set(self._idle, clock, timeout_s)
