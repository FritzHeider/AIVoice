from __future__ import annotations

from typing import Any

import pytest
from service_helpers import settings_of
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from va_common.settings import DatabaseSettings
from va_db.engine import COMMAND_TIMEOUT_S, CONNECT_TIMEOUT_S, DbRole, create_engine

_URL = "postgresql+asyncpg://va:secret@db.internal:5432/va"


def _settings(pool_size: int = 7) -> DatabaseSettings:
    return settings_of(DatabaseSettings, database_url=_URL, database_pool_size=pool_size)


def test_role_values_match_spec_roles() -> None:
    assert {role.value for role in DbRole} == {"va_app", "va_system"}


async def test_engine_uses_settings_url_and_pool_size() -> None:
    engine = create_engine(_settings(pool_size=7), DbRole.APP)
    try:
        assert engine.url.render_as_string(hide_password=False) == _URL
        assert engine.pool.size() == 7  # type: ignore[attr-defined]
    finally:
        await engine.dispose()


def _recorded_kwargs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def recording(url: str, **kwargs: Any) -> AsyncEngine:
        captured.update(kwargs)
        return create_async_engine(url, **kwargs)

    monkeypatch.setattr("va_db.engine.create_async_engine", recording)
    return captured


async def test_login_role_engine_passes_only_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _recorded_kwargs(monkeypatch)
    engine = create_engine(_settings(), None)
    try:
        assert captured["connect_args"] == {
            "timeout": CONNECT_TIMEOUT_S,
            "command_timeout": COMMAND_TIMEOUT_S,
        }
        assert captured["pool_pre_ping"] is True
    finally:
        await engine.dispose()


@pytest.mark.parametrize("role", list(DbRole))
async def test_role_is_a_connection_startup_parameter(monkeypatch: pytest.MonkeyPatch, role: DbRole) -> None:
    captured = _recorded_kwargs(monkeypatch)
    engine = create_engine(_settings(), role)
    try:
        assert captured["connect_args"] == {
            "timeout": CONNECT_TIMEOUT_S,
            "command_timeout": COMMAND_TIMEOUT_S,
            "server_settings": {"role": role.value},
        }
    finally:
        await engine.dispose()
