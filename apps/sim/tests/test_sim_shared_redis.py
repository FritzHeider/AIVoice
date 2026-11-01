"""Two real sim processes sharing one Redis (specs/30 §3 Decision P03-T15c acceptance)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from redis_container import running_redis_url

pytestmark = pytest.mark.integration

_STARTUP_DEADLINE_S = 60.0
_POLL_INTERVAL_S = 0.05
_SCENARIO = b"id: across\nmatch: {to: '+14155550188'}\nanswer: {after_ms: 0}\n"


@pytest.fixture(scope="module")
def redis_url() -> Iterator[str]:
    with running_redis_url() as url:
        yield url


def _free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def _free_base(exclude: set[int]) -> int:
    """A base port whose four simulator ports (base, +100, +200, +300) are all free."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        start = 20_000 + sock.getsockname()[1] % 20_000
    for base in range(start, 60_000):
        ports = {base + offset for offset in (0, 100, 200, 300)}
        if not ports & exclude and all(_free(port) for port in ports):
            return base
    msg = "no free simulator port block"
    raise RuntimeError(msg)


async def _wait_ready(client: httpx.AsyncClient, url: str, process: asyncio.subprocess.Process) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _STARTUP_DEADLINE_S
    while loop.time() < deadline:
        assert process.returncode is None, f"sim process exited with {process.returncode}"
        with contextlib.suppress(httpx.TransportError):
            if (await client.get(f"{url}/readyz")).status_code == 200:
                return
        await asyncio.sleep(_POLL_INTERVAL_S)  # waiting on another process: no event to await
    msg = f"{url} was not ready within {_STARTUP_DEADLINE_S} s"
    raise TimeoutError(msg)


@asynccontextmanager
async def sim_process(redis_url: str, cwd: Path, base: int) -> AsyncIterator[asyncio.subprocess.Process]:
    env = {
        **os.environ,
        "SIM_STATE_REDIS_URL": redis_url,
        "OTEL_TRACES_SAMPLER_ARG": "0",
        "LOG_LEVEL": "WARNING",
    }
    await asyncio.to_thread(cwd.mkdir, parents=True, exist_ok=True)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "va_sim_server.main",
        "--host",
        "127.0.0.1",
        "--port",
        str(base),
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        yield process
    finally:
        if process.returncode is None:
            process.send_signal(signal.SIGTERM)
        await asyncio.wait_for(process.wait(), timeout=30)


async def test_two_sim_processes_share_scenarios_and_reset(redis_url: str, tmp_path: Path) -> None:
    first_base = _free_base(set())
    second_base = _free_base({first_base + offset for offset in (0, 100, 200, 300)})
    first_url, second_url = f"http://127.0.0.1:{first_base}", f"http://127.0.0.1:{second_base}"
    async with (
        sim_process(redis_url, tmp_path / "one", first_base) as first,
        sim_process(redis_url, tmp_path / "two", second_base) as second,
        httpx.AsyncClient(timeout=5) as client,
    ):
        await _wait_ready(client, first_url, first)
        await _wait_ready(client, second_url, second)
        registered = await client.post(
            f"{first_url}/sim/telnyx/scenarios",
            content=_SCENARIO,
            headers={"content-type": "application/yaml"},
        )
        assert registered.status_code == 201
        detail = await client.get(f"{second_url}/sim/telnyx/scenarios/across")
        assert detail.status_code == 200
        assert detail.json()["match"]["to"] == "+14155550188"
        reset = await client.post(f"{second_url}/sim/reset", json={"seed": 11})
        assert reset.status_code == 204
        listing = await client.get(f"{first_url}/sim/telnyx/scenarios")
        assert listing.json() == {"scenarios": []}
        state = await client.get(f"{first_url}/sim/state")
        assert state.json()["seed"] == 11
    assert (first.returncode, second.returncode) == (0, 0)
