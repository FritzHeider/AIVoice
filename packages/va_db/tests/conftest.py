"""Isolate va_db tests from the developer environment and reset the process-wide database."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from docker_probe import docker_available
from pg_container import running_postgres
from service_helpers import isolate_env

from va_common.settings import DatabaseSettings
from va_db.session import configure_database


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    isolate_env(monkeypatch, tmp_path, DatabaseSettings)
    yield
    configure_database(None)


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    """One disposable Postgres per test session (per xdist worker), shared by every integration module."""
    if not docker_available():
        pytest.skip("docker unavailable")
    with running_postgres() as url:
        yield url
