"""Telnyx REST settings and the shared pool/factory helpers (specs/02 §6 ``TELNYX_API_*``, specs/06 §2)."""

from __future__ import annotations

import secrets
import uuid
from pathlib import Path

import fakeredis
import pytest
from clock_helpers import RecordingClock
from dotenv import dotenv_values
from pydantic import ValidationError
from service_helpers import isolate_env, settings_of

from va_telnyx import TelnyxClientFactory
from va_telnyx.settings import (
    SIM_TELNYX_API_KEY,
    TELNYX_HTTP_TIMEOUT,
    TelnyxApiSettings,
    telnyx_factory,
    telnyx_http,
)

ENV_EXAMPLE = Path(__file__).resolve().parents[3] / ".env.example"


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    isolate_env(monkeypatch, tmp_path, TelnyxApiSettings)


def test_defaults_equal_the_env_example_dev_values() -> None:
    settings = settings_of(TelnyxApiSettings)
    example = dotenv_values(ENV_EXAMPLE)
    assert settings.telnyx_api_base == example["TELNYX_API_BASE"]
    assert settings.telnyx_api_key.get_secret_value() == example["TELNYX_API_KEY"] == SIM_TELNYX_API_KEY


def test_environment_overrides_and_hides_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    key = "KEY" + secrets.token_hex(12)
    monkeypatch.setenv("TELNYX_API_KEY", key)
    monkeypatch.setenv("TELNYX_API_BASE", "https://api.telnyx.com/v2")
    settings = settings_of(TelnyxApiSettings)
    assert settings.telnyx_api_key.get_secret_value() == key
    assert settings.telnyx_api_base == "https://api.telnyx.com/v2"
    assert key not in repr(settings)


@pytest.mark.parametrize("env", ["staging", "production"])
def test_deployed_settings_refuse_an_empty_key(env: str) -> None:
    with pytest.raises(ValidationError, match=f"TELNYX_API_KEY must be configured when VA_ENV={env}"):
        settings_of(TelnyxApiSettings, va_env=env, telnyx_api_key="  ")


@pytest.mark.parametrize("env", ["staging", "production"])
def test_deployed_settings_refuse_the_simulator_key(env: str) -> None:
    with pytest.raises(ValidationError, match="TELNYX_API_KEY must not be the simulator key") as caught:
        settings_of(TelnyxApiSettings, va_env=env, telnyx_api_key=SIM_TELNYX_API_KEY)
    assert SIM_TELNYX_API_KEY not in str(caught.value)


@pytest.mark.parametrize("env", ["local", "test"])
def test_dev_settings_accept_the_simulator_key(env: str) -> None:
    assert settings_of(TelnyxApiSettings, va_env=env).telnyx_api_key.get_secret_value() == SIM_TELNYX_API_KEY


async def _no_key(_account_id: uuid.UUID) -> str | None:
    return None


async def test_the_factory_serves_the_platform_client_over_the_shared_pool() -> None:
    key = "KEY" + secrets.token_hex(12)
    settings = settings_of(TelnyxApiSettings, telnyx_api_base="http://telnyx.test/v2", telnyx_api_key=key)
    async with telnyx_http(settings) as http:
        assert http.timeout == TELNYX_HTTP_TIMEOUT
        factory = telnyx_factory(
            settings, http=http, redis=fakeredis.FakeAsyncRedis(), resolve_key=_no_key, clock=RecordingClock()
        )
        assert isinstance(factory, TelnyxClientFactory)
        platform = await factory.for_account(None)
        assert platform is await factory.for_account(None)
