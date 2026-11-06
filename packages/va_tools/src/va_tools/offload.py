"""CPU work on answers, off the event loop (specs/13 §9 Decision P08-FIX-25).

:func:`off_loop` runs a function on the process's ONE reader thread and returns its result.

* One thread per process, shared by every executor and fetcher of the process, jobs in submit order. A
  second CPU-bound thread helps nobody: it only takes the interpreter lock from the first one and from the
  event loop. Measured on the build runner with ten 64 KiB answers searched with ``$..x`` at once, the loop
  was kept waiting 216 ms on the default thread pool (``asyncio.to_thread``), 133 ms with two workers and
  10 ms with one. So never ``asyncio.to_thread`` for this work in a real-time process.
* The caller's context variables are copied to the job, so a line logged from the thread keeps the bound
  ``call_id`` / ``org_id``.
* Cancelling the await returns at once. A job that has not started then never runs; a running one finishes
  and its result (or exception) is dropped.
* Nothing is bounded here, neither the wait for the thread nor the work of one job. The callers bound
  their own wait: :class:`va_tools.http.HttpExecutor` gives a read the request's ``timeout_ms`` (tools,
  Webhook nodes, dynamic data), and a dynamic data fetch, cache hit included, also ends at its budget.
  A caller that gives up cancels its await, so its job never runs if it was still queued. A job that
  is running holds the thread until it is done, whoever still waits for it (``fix:jsonpath-work-bound``).
* ``work`` must be a module function over data (``functools.partial``), never a bound method of a per-call
  object: the queue would keep the call alive and put it on a reference cycle (specs/07 §9).

Creating the executor at import starts no thread; the first job does. The interpreter joins the idle worker
at exit.
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Final

__all__ = ["READER_THREAD", "off_loop"]

READER_THREAD: Final = "va-tools-read"
"""The name prefix of the reader thread (the thread is ``va-tools-read_0``)."""

_READER: Final = ThreadPoolExecutor(max_workers=1, thread_name_prefix=READER_THREAD)


async def off_loop[T](work: Callable[[], T]) -> T:
    """Run ``work`` on the process's one reader thread and return its result (its exception propagates)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_READER, contextvars.copy_context().run, work)
