"""``signal_pipeline_finished`` to ``CallWorkflow`` (specs/07-media-pipeline.md §18 step 5; specs/07 §18
Decision P05-T11b).

The media worker signals the call's workflow (``call-<id>``) with the JSON form of
:class:`~va_domain.call_workflow.PipelineFinished` (``model_dump(mode="json")``, the Temporal default
converter). The signal is best effort: a workflow that does not run any more is logged at info level, every
other failure (connection, RPC) is logged as a warning and never raised, because the workflow's heartbeat
watch and its hangup grace still end the call.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from temporalio.service import RPCError

from va_common.checks import TemporalConnector, signal_workflow
from va_common.logging import get_logger
from va_domain.call_workflow import PIPELINE_FINISHED_SIGNAL, PipelineFinished, call_workflow_id

__all__ = ["PipelineSignaller", "TemporalPipelineSignaller"]

_log = get_logger(__name__)


class PipelineSignaller(Protocol):
    """Tells ``CallWorkflow`` that the conversation ended (never raises)."""

    async def pipeline_finished(self, call_id: uuid.UUID, finished: PipelineFinished) -> None: ...


class TemporalPipelineSignaller:
    """Signals through the lazily connected Temporal client of the media worker."""

    def __init__(self, connector: TemporalConnector) -> None:
        self._connector = connector

    async def pipeline_finished(self, call_id: uuid.UUID, finished: PipelineFinished) -> None:
        try:
            found = await signal_workflow(
                self._connector,
                call_workflow_id(call_id),
                PIPELINE_FINISHED_SIGNAL,
                finished.model_dump(mode="json"),
            )
        except (RPCError, OSError, RuntimeError) as exc:
            _log.warning("media.signal_failed", end_reason=finished.end_reason, error_type=type(exc).__name__)
            return
        if not found:
            _log.info("media.workflow_missing", end_reason=finished.end_reason)
            return
        _log.info("media.pipeline_finished_signalled", end_reason=finished.end_reason)
