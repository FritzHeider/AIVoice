"""Starting ``CallWorkflow`` after the ``calls`` row is committed (specs/05 §5 Decision P05-T05b).

:class:`TemporalCallStarter` connects through the lazily connected :class:`TemporalConnector` and starts
``CallWorkflow`` with id ``call-<call_id>`` on the main task queue, ``REJECT_DUPLICATE`` id reuse. The connect
and the start share one deadline (``start_timeout_s``); the start RPC has its own shorter timeout. A workflow
that already exists counts as started (a retried start). Every other failure is :class:`CallStartError`
(cause chained); the dispatch then marks the call failed. A timed-out start may still have reached Temporal,
so the workflow exits without dialing when the row is no longer ``queued``/``scheduled``.
"""

from __future__ import annotations

from typing import Final, Protocol

from va_common.checks import TemporalConnector, WorkflowStartError, start_workflow
from va_common.logging import get_logger
from va_domain.call_workflow import CALL_WORKFLOW_NAME, CallWorkflowInput

__all__ = ["START_RPC_TIMEOUT_S", "START_TIMEOUT_S", "CallStartError", "CallStarter", "TemporalCallStarter"]

START_TIMEOUT_S: Final = 10.0
START_RPC_TIMEOUT_S: Final = 5.0

_log = get_logger(__name__)


class CallStartError(Exception):
    """``CallWorkflow`` could not be started (Temporal unreachable, refused, or too slow)."""


class CallStarter(Protocol):
    """Starts the workflow of one committed call; raises :class:`CallStartError` on failure."""

    async def start(self, call: CallWorkflowInput) -> None: ...


class TemporalCallStarter:
    """Starts ``CallWorkflow`` on ``task_queue`` through ``connector``."""

    def __init__(
        self, connector: TemporalConnector, task_queue: str, *, start_timeout_s: float = START_TIMEOUT_S
    ) -> None:
        self._connector = connector
        self._task_queue = task_queue
        self._start_timeout_s = start_timeout_s

    async def start(self, call: CallWorkflowInput) -> None:
        try:
            started = await start_workflow(
                self._connector,
                CALL_WORKFLOW_NAME,
                call.model_dump(mode="json"),
                workflow_id=call.workflow_id(),
                task_queue=self._task_queue,
                timeout_s=self._start_timeout_s,
                rpc_timeout_s=START_RPC_TIMEOUT_S,
            )
        except WorkflowStartError as exc:
            msg = f"CallWorkflow for call {call.call_id} could not be started"
            raise CallStartError(msg) from exc.__cause__
        if not started:
            _log.info("calls.workflow_already_started", call_id=str(call.call_id))
