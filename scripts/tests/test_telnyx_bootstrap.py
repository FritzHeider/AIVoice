"""``scripts/telnyx_bootstrap.py``: settings, exit codes, and end-to-end runs against a
FakeTelnyx served on a real socket (specs/06-telephony-telnyx.md §12; Decision 2026-11-01 P03-T15a)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from service_helpers import UNREACHABLE, isolate_env, reset_logging
from sim_helpers import new_state

import telnyx_bootstrap
from va_sim.server import running_sims

__all__ = ["reset_logging"]

IDS = {
    "TELNYX_CALL_CONTROL_APP_ID": "app-1",
    "TELNYX_OUTBOUND_VOICE_PROFILE_ID": "ovp-1",
    "TELNYX_MESSAGING_PROFILE_ID": "mp-1",
}


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No developer ``.env`` or environment variable leaks into a test."""
    isolate_env(monkeypatch, tmp_path, telnyx_bootstrap.BootstrapSettings)


@pytest.fixture
async def telnyx_url() -> AsyncIterator[str]:
    """FakeTelnyx's API base on an ephemeral port."""
    state = new_state()
    try:
        async with running_sims(state, ports={"telnyx": 0}) as urls:
            yield f"{urls['telnyx']}/v2"
    finally:
        await state.aclose()


def _env_file(tmp_path: Path, api_base: str, *extra: str) -> Path:
    path = tmp_path / "bootstrap.env"
    lines = [
        "# local settings",
        "VA_ENV=test",
        f"TELNYX_API_BASE={api_base}",
        f"REDIS_URL={UNREACHABLE['redis_url']}  # the limiter fails open",
        "EDGE_PUBLIC_HOST=edge.example.test",
        *extra,
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_settings_come_from_the_env_file_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _env_file(tmp_path, "http://sim.test/v2", "TELNYX_API_KEY=from-file")
    monkeypatch.setenv("TELNYX_API_KEY", "from-environment")
    settings = telnyx_bootstrap.load_settings(path)
    assert settings.va_env.value == "test"
    assert settings.telnyx_api_base == "http://sim.test/v2"
    assert settings.telnyx_api_key == "from-environment"
    assert settings.edge_public_host == "edge.example.test"
    monkeypatch.delenv("TELNYX_API_KEY")
    defaults = telnyx_bootstrap.load_settings(None)
    assert (defaults.telnyx_api_base, defaults.telnyx_api_key, defaults.edge_public_host) == (
        "http://localhost:9100/v2",
        "sim-telnyx-key",
        "localhost:8100",
    )


def test_missing_env_file_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert telnyx_bootstrap.main(["--env-file", str(tmp_path / "absent.env")]) == telnyx_bootstrap.EXIT_USAGE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "env file not found" in captured.err


def test_invalid_settings_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "bad.env"
    path.write_text("VA_ENV=nowhere\n", encoding="utf-8")
    assert telnyx_bootstrap.main(["--env-file", str(path)]) == telnyx_bootstrap.EXIT_USAGE
    assert "invalid settings" in capsys.readouterr().err


async def test_json_runs_are_idempotent_and_write_the_env_file(
    telnyx_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _env_file(tmp_path, telnyx_url, "TELNYX_CALL_CONTROL_APP_ID=  # platform app")
    argv = ["--env-file", str(path), "--write-env", "--json"]
    assert await asyncio.to_thread(telnyx_bootstrap.main, argv) == 0
    first = json.loads(capsys.readouterr().out)
    assert await asyncio.to_thread(telnyx_bootstrap.main, argv) == 0
    second = json.loads(capsys.readouterr().out)
    assert first["created"] == ["outbound_voice_profile", "call_control_app", "messaging_profile"]
    assert (second["created"], second["updated"]) == ([], [])
    ids = {key: first[key] for key in IDS}
    assert all(ids.values())
    assert {key: second[key] for key in IDS} == ids
    lines = path.read_text(encoding="utf-8").splitlines()
    assert f"TELNYX_CALL_CONTROL_APP_ID={ids['TELNYX_CALL_CONTROL_APP_ID']}  # platform app" in lines
    assert f"TELNYX_OUTBOUND_VOICE_PROFILE_ID={ids['TELNYX_OUTBOUND_VOICE_PROFILE_ID']}" in lines
    assert f"TELNYX_MESSAGING_PROFILE_ID={ids['TELNYX_MESSAGING_PROFILE_ID']}" in lines
    assert lines[0] == "# local settings"


async def test_text_output_is_three_assignments(
    telnyx_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _env_file(tmp_path, telnyx_url)
    before = path.read_text(encoding="utf-8")
    assert await asyncio.to_thread(telnyx_bootstrap.main, ["--env-file", str(path)]) == 0
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert [line.split("=", 1)[0] for line in lines] == list(IDS)
    assert all(line.split("=", 1)[1] for line in lines)
    assert path.read_text(encoding="utf-8") == before
    assert "telnyx_bootstrap.done" in captured.err


async def test_default_env_file_is_used_and_written(
    telnyx_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _env_file(tmp_path, telnyx_url).rename(tmp_path / ".env")
    assert await asyncio.to_thread(telnyx_bootstrap.main, ["--write-env"]) == 0
    printed = capsys.readouterr().out.splitlines()
    assert set(printed) <= set((tmp_path / ".env").read_text(encoding="utf-8").splitlines())


async def test_telnyx_failure_exits_1_without_leaking_the_key(
    telnyx_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "KEY-not-registered-at-the-sim"  # noqa: S105 - a fake key the sim rejects
    path = _env_file(tmp_path, telnyx_url, f"TELNYX_API_KEY={secret}")
    assert await asyncio.to_thread(telnyx_bootstrap.main, ["--env-file", str(path), "--write-env"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "telnyx_bootstrap.failed" in captured.err
    assert "401" in captured.err
    assert secret not in captured.err
    assert "TELNYX_CALL_CONTROL_APP_ID" not in path.read_text(encoding="utf-8")


async def test_local_and_test_envs_register_plain_http_webhooks(
    telnyx_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _env_file(tmp_path, telnyx_url)
    assert await asyncio.to_thread(telnyx_bootstrap.main, ["--env-file", str(path), "--json"]) == 0
    app_id = json.loads(capsys.readouterr().out)["TELNYX_CALL_CONTROL_APP_ID"]
    headers = {"Authorization": "Bearer sim-telnyx-key"}
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(f"{telnyx_url}/call_control_applications/{app_id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["data"]["webhook_event_url"] == "http://edge.example.test/telnyx/voice"
