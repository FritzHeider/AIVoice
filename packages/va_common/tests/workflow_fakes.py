"""A stand-in Temporal client for code that signals workflows through ``va_common.checks.signal_workflow``."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final, cast

from temporalio.client import Client
from temporalio.converter import DataConverter

from va_common.checks import TemporalConnector

__all__ = ["NO_ARG", "SignalRecorder", "connector_of"]

NO_ARG: Final = object()
"""Recorded as the argument of a signal sent without one."""


class SignalRecorder:
    """Records ``(workflow id, signal, args)`` for every signal and raises ``error`` after recording it.

    ``args`` is the tuple of arguments sent: ``(arg,)`` for a positional argument, the ``args=`` list
    otherwise (empty for a signal without an argument).
    """

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.sent: list[tuple[str, str, tuple[Any, ...]]] = []
        self._workflow_id = ""

    def get_workflow_handle(self, workflow_id: str) -> SignalRecorder:
        self._workflow_id = workflow_id
        return self

    async def signal(self, name: str, arg: Any = NO_ARG, *, args: Sequence[Any] = ()) -> None:
        sent = tuple(args) if arg is NO_ARG else (arg,)
        self.sent.append((self._workflow_id, name, sent))
        if self.error is not None:
            raise self.error


def connector_of(client: SignalRecorder, connect_error: BaseException | None = None) -> TemporalConnector:
    """A connector handing out ``client`` (or raising ``connect_error`` on connect)."""

    async def connect(_host: str, _namespace: str, _converter: DataConverter | None) -> Client:
        if connect_error is not None:
            raise connect_error
        return cast("Client", client)

    return TemporalConnector("temporal.test:7233", "default", connect=connect)
