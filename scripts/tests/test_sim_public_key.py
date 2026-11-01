"""``scripts/sim_public_key.py``: key fetching with retries, validation, the env/key file writes, and an
end-to-end run against FakeTelnyx on a real socket (specs/29 §2 Decision P03-T15b)."""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
from pathlib import Path

import httpx
import pytest
from service_helpers import UNREACHABLE, isolate_env
from sim_helpers import new_state

import sim_public_key as spk
from va_sim.server import running_sims

SIM = "http://sim.test:9100"


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No developer ``.env`` or environment variable leaks into a test."""
    isolate_env(monkeypatch, tmp_path, spk.SimKeySettings)


def _key() -> str:
    return base64.b64encode(secrets.token_bytes(spk.ED25519_PUBLIC_KEY_BYTES)).decode()


class Script:
    """A transport answering the queued items in order: an exception to raise or a response to return."""

    def __init__(self, *items: httpx.Response | Exception) -> None:
        self.items = list(items)
        self.urls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


async def _fetch(script: Script, sleeps: Sleeps, *, attempts: int = 3) -> str:
    async with httpx.AsyncClient(transport=httpx.MockTransport(script)) as http:
        return await spk.fetch_public_key(http, f"{SIM}/", attempts=attempts, delay_s=1.0, sleep=sleeps)


async def test_fetch_returns_the_key() -> None:
    key = _key()
    script, sleeps = Script(httpx.Response(200, json={"public_key": key})), Sleeps()
    assert await _fetch(script, sleeps) == key
    assert script.urls == [f"{SIM}/sim/telnyx/public-key"]
    assert sleeps.calls == []


async def test_fetch_retries_transport_errors_until_the_sim_answers() -> None:
    key = _key()
    script = Script(
        httpx.ConnectError("refused"),
        httpx.ReadError("reset"),
        httpx.Response(200, json={"public_key": key}),
    )
    sleeps = Sleeps()
    assert await _fetch(script, sleeps) == key
    assert sleeps.calls == [1.0, 1.0]


async def test_fetch_gives_up_after_the_last_attempt() -> None:
    script, sleeps = Script(*(httpx.ConnectError("refused") for _ in range(3))), Sleeps()
    with pytest.raises(spk.SimKeyError, match="unreachable after 3 attempts \\(ConnectError\\)"):
        await _fetch(script, sleeps)
    assert sleeps.calls == [1.0, 1.0]


async def test_fetch_needs_at_least_one_attempt() -> None:
    with pytest.raises(spk.SimKeyError, match="at least 1"):
        await _fetch(Script(), Sleeps(), attempts=0)


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(503, json={"public_key": "x"}), "HTTP 503"),
        (httpx.Response(200, content=b"<html>"), "non-JSON"),
        (httpx.Response(200, json=["not", "an", "object"]), "no public_key string"),
        (httpx.Response(200, json={"public_key": 7}), "no public_key string"),
        (httpx.Response(200, json={"public_key": "***"}), "not valid base64"),
        (
            httpx.Response(200, json={"public_key": base64.b64encode(b"short").decode()}),
            "5 bytes, expected 32",
        ),
    ],
    ids=["status", "html", "list", "number", "base64", "length"],
)
async def test_fetch_rejects_bad_answers_without_retrying(response: httpx.Response, message: str) -> None:
    script, sleeps = Script(response), Sleeps()
    with pytest.raises(spk.SimKeyError, match=message):
        await _fetch(script, sleeps)
    assert sleeps.calls == []


def test_write_key_file_creates_parents(tmp_path: Path) -> None:
    key = _key()
    path = tmp_path / ".sim" / "telnyx_public_key"
    spk.write_key_file(path, key)
    assert path.read_text(encoding="utf-8") == f"{key}\n"


def _env_file(tmp_path: Path) -> Path:
    path = tmp_path / "dev.env"
    path.write_text(
        "# local\nTELNYX_API_KEY=sim-telnyx-key\nTELNYX_PUBLIC_KEY=  # Ed25519 public key\nVA_ENV=local\n",
        encoding="utf-8",
    )
    return path


async def test_main_writes_the_running_sims_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env_file, key_file = _env_file(tmp_path), tmp_path / "keys" / "telnyx_public_key"
    state = new_state()
    try:
        async with running_sims(state, ports={"telnyx": 0}) as urls:
            argv = ["--url", urls["telnyx"], "--env-file", str(env_file), "--key-file", str(key_file)]
            assert await asyncio.to_thread(spk.main, argv) == 0
            first = capsys.readouterr().out
            assert await asyncio.to_thread(spk.main, argv) == 0
            second = capsys.readouterr().out
        key = state.signing_key.public_key_b64
    finally:
        await state.aclose()
    assert env_file.read_text(encoding="utf-8") == (
        "# local\nTELNYX_API_KEY=sim-telnyx-key\n"
        f"TELNYX_PUBLIC_KEY={key}  # Ed25519 public key\nVA_ENV=local\n"
    )
    assert key_file.read_text(encoding="utf-8") == f"{key}\n"
    assert "TELNYX_PUBLIC_KEY updated" in first
    assert "TELNYX_PUBLIC_KEY unchanged" in second


def test_main_fails_and_leaves_files_alone_when_the_sim_is_down(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file, key_file = _env_file(tmp_path), tmp_path / "telnyx_public_key"
    before = env_file.read_text(encoding="utf-8")
    os.utime(env_file, (1_000_000, 1_000_000))
    unreachable = UNREACHABLE["clickhouse_url"].removesuffix("/va")
    argv = ["--url", unreachable, "--env-file", str(env_file), "--key-file", str(key_file), "--attempts", "1"]
    assert spk.main(argv) == spk.EXIT_FAILED
    assert "unreachable after 1 attempts" in capsys.readouterr().err
    assert env_file.read_text(encoding="utf-8") == before
    assert env_file.stat().st_mtime == 1_000_000
    assert not key_file.exists()
