"""Edge settings defaults (specs/02-repo-layout-and-conventions.md §6)."""

from __future__ import annotations

from pathlib import Path

import pytest
from service_helpers import settings_of

from va_edge.settings import SIM_PUBLIC_KEY_FILE, EdgeSettings


def test_defaults_match_spec() -> None:
    settings = settings_of(EdgeSettings)
    assert settings.service_name == "telephony-edge"
    assert settings.database_url == "postgresql+asyncpg://va:va@localhost:5432/va"
    assert settings.database_pool_size == 10
    assert settings.redis_url == "redis://localhost:6379/0"
    assert (settings.temporal_host, settings.temporal_namespace) == ("localhost:7233", "default")
    assert settings.temporal_task_queue == "va-main"
    assert (settings.edge_public_host, settings.media_public_host) == ("localhost:8100", "localhost:8200")
    assert settings.telnyx_public_key == ""
    assert settings.telnyx_api_base == "http://localhost:9100/v2"
    assert settings.telnyx_api_key.get_secret_value() == "sim-telnyx-key"
    assert (settings.kms_provider.value, settings.local_master_key.get_secret_value()) == ("local", "")


def test_staging_refuses_the_simulator_telnyx_key() -> None:
    with pytest.raises(ValueError, match="TELNYX_API_KEY must not be the simulator key"):
        settings_of(EdgeSettings, va_env="staging", kms_provider="aws", aws_kms_key_id="arn:aws:kms:key")


def test_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDGE_PUBLIC_HOST", "edge.example.ngrok.app")
    monkeypatch.setenv("DATABASE_POOL_SIZE", "3")
    settings = settings_of(EdgeSettings)
    assert settings.edge_public_host == "edge.example.ngrok.app"
    assert settings.database_pool_size == 3


def test_webhook_public_key_prefers_the_variable(tmp_path: Path) -> None:
    key_file = tmp_path / "telnyx_public_key"
    key_file.write_text("from-file\n", encoding="utf-8")
    settings = settings_of(EdgeSettings, telnyx_public_key=" configured ")
    assert settings.webhook_public_key(key_file) == "configured"


def test_webhook_public_key_falls_back_to_the_sim_file_in_local(tmp_path: Path) -> None:
    key_file = tmp_path / "telnyx_public_key"
    assert settings_of(EdgeSettings).webhook_public_key(key_file) == ""
    key_file.write_text("from-file\n", encoding="utf-8")
    assert settings_of(EdgeSettings).webhook_public_key(key_file) == "from-file"
    assert settings_of(EdgeSettings, va_env="test").webhook_public_key(key_file) == ""


def test_webhook_public_key_default_file_is_relative_to_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert Path(".sim/telnyx_public_key") == SIM_PUBLIC_KEY_FILE
    (tmp_path / ".sim").mkdir()
    (tmp_path / ".sim" / "telnyx_public_key").write_text("sim-key", encoding="utf-8")
    assert settings_of(EdgeSettings).webhook_public_key() == "sim-key"
