"""Readiness check runner (specs/27-observability.md §9)."""

from __future__ import annotations

import asyncio
import io
import json
from typing import Any

import pytest

from va_common.health import CheckFn, CheckResult, CheckStatus, ReadinessReport, liveness_body, run_checks
from va_common.logging import configure_logging
from va_common.settings import BaseServiceSettings


async def ok() -> None:
    return None


async def broken() -> None:
    msg = "password=hunter2 at db:5432"
    raise ConnectionRefusedError(msg)


async def hangs() -> None:
    await asyncio.Event().wait()


async def test_all_ok() -> None:
    report = await run_checks({"postgres": ok, "redis": ok}, version="1.2.3")
    assert report.ok
    assert report.failing == ()
    assert report.http_status == 200
    assert report.body() == {"status": "ok", "checks": {"postgres": "ok", "redis": "ok"}, "version": "1.2.3"}


async def test_error_detail_is_type_name_and_logged() -> None:
    stream = io.StringIO()
    kwargs: dict[str, Any] = {"_env_file": None, "log_format": "json"}
    settings = BaseServiceSettings(**kwargs)
    configure_logging(settings, stream=stream, environ={}, pod="p")
    report = await run_checks({"redis": broken, "postgres": ok}, version="v")
    assert report.results == (
        CheckResult("redis", CheckStatus.ERROR, "ConnectionRefusedError"),
        CheckResult("postgres", CheckStatus.OK),
    )
    assert report.http_status == 503
    assert "hunter2" not in json.dumps(report.body())
    line = json.loads(stream.getvalue().splitlines()[-1])
    assert line["event"] == "health.check_failed"
    assert (line["check"], line["detail"]) == ("redis", "ConnectionRefusedError")


async def test_timeout() -> None:
    report = await run_checks({"temporal": hangs}, timeout_s=0.01, version="v")
    assert report.results == (CheckResult("temporal", CheckStatus.TIMEOUT, "timeout"),)


async def test_mixed_body_lists_failing_sorted() -> None:
    report = await run_checks({"z": broken, "a": hangs, "m": ok}, timeout_s=0.01, version="v")
    assert report.failing == ("a", "z")
    assert report.body() == {
        "status": "unavailable",
        "checks": {"z": "error", "a": "timeout", "m": "ok"},
        "failing": ["a", "z"],
        "version": "v",
    }


async def test_checks_run_concurrently() -> None:
    gate = asyncio.Event()
    entered: list[str] = []

    def waiting(name: str) -> CheckFn:
        async def check() -> None:
            entered.append(name)
            if len(entered) == 2:
                gate.set()
            await gate.wait()

        return check

    report = await run_checks({"a": waiting("a"), "b": waiting("b")}, timeout_s=1.0, version="v")
    assert report.ok


async def test_cancellation_propagates() -> None:
    task = asyncio.create_task(run_checks({"slow": hangs}, timeout_s=10.0, version="v"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_empty_checks_ok() -> None:
    report = await run_checks({}, version="v")
    assert report == ReadinessReport((), "v")
    assert report.body() == {"status": "ok", "checks": {}, "version": "v"}


def test_liveness_body() -> None:
    assert liveness_body("9.9.9") == {"status": "ok", "version": "9.9.9"}
