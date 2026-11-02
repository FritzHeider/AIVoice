"""Waiting on an event or a future with a deadline on the injected clock (never ``asyncio.wait_for``).

Shared by the call pipeline (``va_voice.pipeline.waits`` re-exports it), the media worker, and the telephony
edge's inbound accept.

``asyncio.wait_for`` times out on the event loop's clock; the deadlines here live on an injected
:class:`~va_common.time.Clock`, so a test clock (or the simulators' accelerated clock) controls them.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable

from va_common.time import Clock

__all__ = ["wait_until_set", "within"]


async def within(awaitable: Awaitable[object], clock: Clock, timeout_s: float | None) -> bool:
    """Await ``awaitable`` for at most ``timeout_s`` seconds of ``clock`` time; ``True`` when it finished.

    On timeout (or when the caller is cancelled) the awaitable is cancelled. Its exception, if it fails first,
    propagates.
    """
    task = asyncio.ensure_future(awaitable)
    if timeout_s is None:
        await task
        return True
    sleeper = asyncio.ensure_future(clock.sleep(max(timeout_s, 0.0)))
    try:
        done, _ = await asyncio.wait({task, sleeper}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for pending in (task, sleeper):
            if not pending.done():
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pending
    if task in done:
        task.result()
        return True
    return False


async def wait_until_set(event: asyncio.Event, clock: Clock, timeout_s: float | None) -> bool:
    """Wait for ``event`` for at most ``timeout_s`` seconds of ``clock`` time; ``True`` when it is set."""
    if event.is_set():
        return True
    return await within(event.wait(), clock, timeout_s)
