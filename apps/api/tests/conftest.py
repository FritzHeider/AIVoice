"""API test fixtures: isolated environment, log reset, default settings, and the session integration fixtures.

The session fixtures (``migrated_url`` per module, ``identity``, ``container``, ``client``) start a Postgres
container only when a test requests them; modules that define their own fixtures of these names override them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from api_helpers import make_settings
from identity_db import IdentityDb, open_identity_db
from service_helpers import isolate_env, reset_logging
from session_helpers import SessionContainer, migrated_postgres, session_app, session_client

from va_api.settings import ApiSettings

__all__ = ["reset_logging"]


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("POD_ID", raising=False)
    isolate_env(monkeypatch, tmp_path, ApiSettings)


@pytest.fixture
def settings() -> ApiSettings:
    return make_settings()


@pytest.fixture(scope="module")
def migrated_url() -> Iterator[str]:
    with migrated_postgres() as url:
        yield url


@pytest.fixture
async def identity(migrated_url: str) -> AsyncIterator[IdentityDb]:
    async with open_identity_db(migrated_url) as db:
        yield db


@pytest.fixture
def container(identity: IdentityDb) -> SessionContainer:
    return SessionContainer(identity=identity)


@pytest.fixture
async def client(container: SessionContainer) -> AsyncIterator[httpx.AsyncClient]:
    async with session_client(session_app(container)) as http:
        yield http
