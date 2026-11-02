"""Result of ``CallWorkflow`` (specs/01-architecture.md §2.1 Decision P05-T06c).

Kept in its own import-free module so the workflow can import it through the sandbox pass-through.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

__all__ = ["CallOutcome", "CallWorkflowResult"]

type CallOutcome = Literal[
    "completed",
    "canceled",
    "not_found",
    "skipped",
    "blocked",
    "failed",
    "error",
    "not_answered",
    "fax_detected",
    "stream_failed",
    "worker_crash",
    "max_duration",
]
"""How the workflow saw the call end: ``completed`` (the conversation ran and ended), ``canceled``
(stopped before the answer), ``not_found`` (no such call), ``skipped`` (already terminal before the dial),
``blocked`` / ``failed`` (dispatch re-check / Telnyx rejected the dial), ``error`` (dial or ring backstop
failure), ``not_answered`` (the carrier hung up before the answer), and the in-call backstops
``fax_detected``, ``stream_failed``, ``worker_crash``, ``max_duration``."""


class CallWorkflowResult(BaseModel):
    """``end_reason`` is the reason the workflow recorded itself (``None`` when another component, or an
    earlier step, had already ended the call)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: CallOutcome
    end_reason: str | None = None
