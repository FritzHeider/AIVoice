"""va_llm test fixtures: an isolated environment, log reset, and respx routes for the test providers."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import openai._base_client
import pytest
import respx
from llm_client_helpers import EmbeddingsRig, GatewayRig, embeddings_rig, gateway_rig
from service_helpers import isolate_env, reset_logging

from va_common.settings import DatabaseSettings
from va_llm import LlmSettings, structured

__all__ = [
    "erig",
    "fresh_structured_registry",
    "inline_sdk_platform_lookup",
    "isolated_env",
    "llm_api",
    "reset_logging",
    "rig",
]


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No ``LLM_*``/database variable or developer ``.env`` leaks into a test."""
    isolate_env(monkeypatch, tmp_path, LlmSettings, DatabaseSettings)


@pytest.fixture(autouse=True)
def fresh_structured_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with an empty structured-function registry (registrations are process-global)."""
    monkeypatch.setattr(structured, "_registry", {})


def _inline[T](function: Callable[..., T]) -> Callable[..., Awaitable[T]]:
    async def run(*args: Any, **kwargs: Any) -> T:
        return function(*args, **kwargs)

    return run


@pytest.fixture(autouse=True)
def inline_sdk_platform_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``openai`` SDK looks up the platform in a worker thread before a client's first request. Under
    load that wall-clock hop could outlast a ``ManualClock`` deadline the test runs, so the request was
    cancelled before it reached respx (and a later request took its scripted answer). Running the lookup
    inline keeps every request path on the event loop."""
    monkeypatch.setattr(openai._base_client, "asyncify", _inline)


@pytest.fixture
def llm_api() -> Iterator[respx.MockRouter]:
    """respx routes (requests to unrouted hosts fail the test)."""
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
async def rig() -> AsyncIterator[GatewayRig]:
    async with gateway_rig() as gateway:
        yield gateway


@pytest.fixture
async def erig() -> AsyncIterator[EmbeddingsRig]:
    async with embeddings_rig() as embeddings:
        yield embeddings
