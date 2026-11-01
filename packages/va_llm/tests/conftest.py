"""va_llm test fixtures: an isolated environment, log reset, and respx routes for the test providers."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import respx
from llm_client_helpers import GatewayRig, gateway_rig
from service_helpers import isolate_env, reset_logging

from va_common.settings import DatabaseSettings
from va_llm import LlmSettings

__all__ = ["isolated_env", "llm_api", "reset_logging", "rig"]


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No ``LLM_*``/database variable or developer ``.env`` leaks into a test."""
    isolate_env(monkeypatch, tmp_path, LlmSettings, DatabaseSettings)


@pytest.fixture
def llm_api() -> Iterator[respx.MockRouter]:
    """respx routes (requests to unrouted hosts fail the test)."""
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
async def rig() -> AsyncIterator[GatewayRig]:
    async with gateway_rig() as gateway:
        yield gateway
