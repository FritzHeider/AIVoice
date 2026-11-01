"""Fakes of the Send Call container pieces: a recording ``CallWorkflow`` starter and a DNS resolver."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from va_api.services.call_starter import CallStartError
from va_domain.call_workflow import CallWorkflowInput

__all__ = ["PUBLIC_ADDRESS", "FakeCallStarter", "FakeResolver"]

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
class FakeResolver:
    """Answers ``answers[host]`` (default: one public address); records every lookup."""

    answers: dict[str, list[str]] = field(default_factory=dict)
    lookups: list[str] = field(default_factory=list)

    async def __call__(self, host: str) -> list[str]:
        self.lookups.append(host)
        return self.answers.get(host, [PUBLIC_ADDRESS])
