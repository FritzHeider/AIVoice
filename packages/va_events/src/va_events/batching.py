"""Shared trigger wait of the write-behind buffers (the ``call_events`` buffer here, the media worker's turn
writer)."""

from __future__ import annotations

import asyncio

from va_common.time import Clock

__all__ = ["wait_for_trigger"]


async def wait_for_trigger(clock: Clock, wake: asyncio.Event, interval_s: float) -> None:
    """Return after ``interval_s`` on ``clock`` or as soon as ``wake`` is set, whichever comes first."""
    sleeper = asyncio.ensure_future(clock.sleep(interval_s))
    woken = asyncio.ensure_future(wake.wait())
    try:
        await asyncio.wait({sleeper, woken}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        sleeper.cancel()
        woken.cancel()
        await asyncio.gather(sleeper, woken, return_exceptions=True)
