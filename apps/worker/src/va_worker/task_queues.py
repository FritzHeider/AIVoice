"""Temporal task queues hosted by the worker (specs/01-architecture.md §1).

``TEMPORAL_TASK_QUEUE`` names the main queue (``va-main``); the post-call, ingest, and system queues are
derived by replacing its ``-main`` suffix. Workflow packages append their
:class:`TaskQueueRegistration` entries to :data:`REGISTRATIONS`; a queue without registrations is not polled.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

__all__ = ["MAIN_SUFFIX", "QUEUE_SUFFIXES", "REGISTRATIONS", "TaskQueueRegistration", "task_queue_names"]

MAIN_SUFFIX: Final = "-main"
QUEUE_SUFFIXES: Final = ("-main", "-postcall", "-ingest", "-system")


def task_queue_names(base: str) -> tuple[str, ...]:
    """``va-main`` → ``("va-main", "va-postcall", "va-ingest", "va-system")``."""
    if not base.endswith(MAIN_SUFFIX) or base == MAIN_SUFFIX:
        msg = f"TEMPORAL_TASK_QUEUE must end with {MAIN_SUFFIX!r}, got {base!r}"
        raise ValueError(msg)
    prefix = base.removesuffix(MAIN_SUFFIX)
    return tuple(f"{prefix}{suffix}" for suffix in QUEUE_SUFFIXES)


@dataclass(frozen=True, slots=True)
class TaskQueueRegistration:
    """Workflows and activities polled on one task queue."""

    task_queue: str
    workflows: Sequence[type]
    activities: Sequence[Callable[..., Any]]


# No workflow exists before the workflow phases; `va_workflows` registers its queues here.
REGISTRATIONS: Final[tuple[TaskQueueRegistration, ...]] = ()
