"""Fakes of the Send Call container pieces: a recording ``CallWorkflow`` starter, a recording canceller, and
a DNS resolver."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final
from uuid import UUID

from va_api.services.call_starter import CallStartError
from va_api.services.call_stop import CallCancelError
from va_domain.call_workflow import CallWorkflowInput

__all__ = ["PUBLIC_ADDRESS", "FakeCallCanceller", "FakeCallStarter", "FakeResolver"]

PUBLIC_ADDRESS: Final = "8.8.8.8"


@dataclass
class FakeCallStarter:
    """Records every start; raises ``fail`` (when set) instead of starting."""

    started: list[CallWorkflowInput] = field(default_factory=list)
    fail: CallStartError | None = None

    async def start(self, call: CallWorkflowInput) -> None:
        if self.fail is not None:
            raise self.fail
        self.started.append(call)


@dataclass
class FakeCallCanceller:
    """Records every cancel; answers ``False`` (no workflow) when ``missing``, raises ``fail`` when set."""

    cancelled: list[UUID] = field(default_factory=list)
    missing: bool = False
    fail: CallCancelError | None = None

    async def cancel(self, call_id: UUID) -> bool:
        if self.fail is not None:
            raise self.fail
        self.cancelled.append(call_id)
        return not self.missing


@dataclass
class FakeResolver:
    """Answers ``answers[host]`` (default: one public address); records every lookup."""

    answers: dict[str, list[str]] = field(default_factory=dict)
    lookups: list[str] = field(default_factory=list)

    async def __call__(self, host: str) -> list[str]:
        self.lookups.append(host)
        return self.answers.get(host, [PUBLIC_ADDRESS])
