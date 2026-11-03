"""Test fixtures: isolated environment, log reset, and a migrated Postgres per xdist worker."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from migration_helpers import migrated_postgres
from service_helpers import isolate_env, reset_logging

from va_webhooks.settings import DispatcherSettings

__all__ = ["reset_logging"]


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    isolate_env(monkeypatch, tmp_path, DispatcherSettings)


@pytest.fixture(scope="session")
def dispatcher_db_url() -> Iterator[str]:
    with migrated_postgres() as url:
        yield url
