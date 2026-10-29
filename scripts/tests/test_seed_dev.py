"""``scripts/seed_dev.py``: argument handling, environment refusal, and output (the seeding itself is covered
against Postgres by ``apps/api/tests/test_dev_seed.py``)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

import seed_dev
from va_api.dev_seed import (
    DEV_CREDITS_MICROS,
    DEV_EMAIL,
    DEV_ORG_NAME,
    DEV_PLAN_ID,
    SEED_REFUSED_MESSAGE,
    SeededKey,
    SeedResult,
)
from va_api.settings import ApiSettings
from va_common.errors import AppError

PASSWORD = "generated-password"  # noqa: S105 - a fake seed result's password
RESULT = SeedResult(
    created=True,
    org_id=uuid.UUID(int=1),
    org_name=DEV_ORG_NAME,
    slug="voiceagents-dev-ab12",
    plan_id=DEV_PLAN_ID,
    credits_balance_micros=DEV_CREDITS_MICROS,
    owner_id=uuid.UUID(int=2),
    owner_email=DEV_EMAIL,
    password=PASSWORD,
    keys={
        "live": SeededKey(id=uuid.UUID(int=3), prefix="va_live_aaaa1111", key="va_live_plaintextlive"),
        "test": SeededKey(id=uuid.UUID(int=4), prefix="va_test_bbbb2222", key="va_test_plaintexttest"),
    },
    revoked_prefixes=["va_test_cccc3333"],
)


class FakeRun:
    """Stands in for ``seed_dev.run_seed``; records its arguments and returns or raises as told."""

    def __init__(self, failed: list[str] | None = None, error: Exception | None = None) -> None:
        self.failed = failed or []
        self.error = error
        self.calls: list[tuple[str, bool]] = []

    async def __call__(
        self, _settings: ApiSettings, *, email: str, reset_password: bool
    ) -> tuple[SeedResult, list[str]]:
        self.calls.append((email, reset_password))
        if self.error is not None:
            raise self.error
        return RESULT, self.failed


@pytest.fixture(autouse=True)
def local_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No developer ``.env``; ``VA_ENV=local`` unless a test overrides it."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VA_ENV", "local")


def use(monkeypatch: pytest.MonkeyPatch, run: FakeRun) -> FakeRun:
    monkeypatch.setattr(seed_dev, "run_seed", run)
    return run


def test_json_output_is_one_object(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run = use(monkeypatch, FakeRun())
    assert seed_dev.main(["--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == RESULT.to_json()
    assert captured.err == ""
    assert run.calls == [(DEV_EMAIL, False)]


def test_text_output_and_options(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    run = use(monkeypatch, FakeRun())
    assert seed_dev.main(["--email", "me@voiceagents.test", "--reset-password"]) == 0
    out = capsys.readouterr().out
    assert f"password: {PASSWORD}" in out
    assert 'curl -H "Authorization: va_test_plaintexttest" localhost:8000/v1/me' in out
    assert run.calls == [("me@voiceagents.test", True)]


def test_cache_failures_warn_on_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use(monkeypatch, FakeRun(failed=["va_test_cccc3333"]))
    assert seed_dev.main(["--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["created"] is True
    assert "va_test_cccc3333" in captured.err


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_refuses_outside_local_and_test(
    environment: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VA_ENV", environment)
    run = use(monkeypatch, FakeRun())
    assert seed_dev.main([]) == seed_dev.EXIT_REFUSED
    captured = capsys.readouterr()
    assert (captured.out, captured.err.strip()) == ("", SEED_REFUSED_MESSAGE)
    assert run.calls == []


@pytest.mark.parametrize(
    "error",
    [
        AppError("INVALID_INPUT", "The email address is not valid.", field="email"),
        RuntimeError("no master key"),
    ],
)
def test_failures_exit_one(
    error: Exception, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use(monkeypatch, FakeRun(error=error))
    assert seed_dev.main(["--json"]) == seed_dev.EXIT_FAILED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("seed failed: ")


def test_invalid_settings_exit_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_POOL_SIZE", "0")
    run = use(monkeypatch, FakeRun())
    assert seed_dev.main([]) == seed_dev.EXIT_FAILED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "database_pool_size" in captured.err
    assert run.calls == []
