"""Readiness checks shared by every service (specs/27-observability.md §9).

A check is an async callable that returns on success and raises on failure. :func:`run_checks` runs all checks
concurrently, each bounded by ``timeout_s``, and returns a :class:`ReadinessReport` whose
:meth:`~ReadinessReport.body` and :attr:`~ReadinessReport.http_status` are the ``/readyz`` response. Failure
details are logged, never returned.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from va_common.logging import get_logger

__all__ = [
    "CheckFn",
    "CheckResult",
    "CheckStatus",
    "ReadinessReport",
    "liveness_body",
    "run_checks",
]

type CheckFn = Callable[[], Awaitable[None]]

_HTTP_OK: Final = 200
_HTTP_UNAVAILABLE: Final = 503

_log = get_logger(__name__)


class CheckStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: CheckStatus
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    results: tuple[CheckResult, ...]
    version: str

    @property
    def ok(self) -> bool:
        return all(result.status is CheckStatus.OK for result in self.results)

    @property
    def failing(self) -> tuple[str, ...]:
        return tuple(sorted(r.name for r in self.results if r.status is not CheckStatus.OK))

    @property
    def http_status(self) -> int:
        return _HTTP_OK if self.ok else _HTTP_UNAVAILABLE

    def body(self) -> dict[str, Any]:
        """``{"status","checks","version"}``, plus ``failing`` (sorted names) when any check failed."""
        checks = {result.name: result.status.value for result in self.results}
        if self.ok:
            return {"status": "ok", "checks": checks, "version": self.version}
        return {
            "status": "unavailable",
            "checks": checks,
            "failing": list(self.failing),
            "version": self.version,
        }


def liveness_body(version: str) -> dict[str, str]:
    """``/healthz`` body: the process is up."""
    return {"status": "ok", "version": version}


async def _run_one(name: str, check: CheckFn, timeout_s: float) -> CheckResult:
    try:
        async with asyncio.timeout(timeout_s):
            await check()
    except TimeoutError:
        _log.warning("health.check_failed", check=name, detail="timeout")
        return CheckResult(name, CheckStatus.TIMEOUT, "timeout")
    except Exception as exc:  # a readiness check reports any failure instead of raising
        detail = type(exc).__name__
        _log.warning("health.check_failed", check=name, detail=detail)
        return CheckResult(name, CheckStatus.ERROR, detail)
    return CheckResult(name, CheckStatus.OK)


async def run_checks(
    checks: Mapping[str, CheckFn], *, timeout_s: float = 1.0, version: str
) -> ReadinessReport:
    """Run ``checks`` concurrently, each within ``timeout_s`` seconds; results keep the mapping's order."""
    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(_run_one(name, check, timeout_s)) for name, check in checks.items()]
    return ReadinessReport(tuple(task.result() for task in tasks), version)
