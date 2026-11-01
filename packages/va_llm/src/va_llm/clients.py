"""Cached ``AsyncOpenAI`` clients on one shared httpx client (specs/10 §1; Decision P03-T13a rule 2).

One client per provider id; a provider whose base URL or key changed (a rotated org key) gets a new client.
The cache never closes clients: ``AsyncOpenAI.close()`` would close the shared httpx client, which the caller
owns.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Final

import httpx
import openai

from va_common.logging import get_logger
from va_llm.providers import LlmProvider

__all__ = ["CALL_ID_HEADER", "CLIENT_TIMEOUT", "LlmClients", "openai_client", "sdk_timeout"]

CALL_ID_HEADER: Final = "X-VA-Call-Id"
CLIENT_TIMEOUT: Final = httpx.Timeout(connect=2.0, read=30.0, write=5.0, pool=2.0)
_PREWARM_TIMEOUT: Final = httpx.Timeout(connect=2.0, read=5.0, write=5.0, pool=2.0)

_log = get_logger(__name__)


def sdk_timeout(timeout: httpx.Timeout) -> Any:
    """``timeout`` for an SDK call: openai >= 3 is typed against ``httpx2`` but accepts httpx objects."""
    return timeout


def openai_client(
    provider: LlmProvider, http: httpx.AsyncClient, *, timeout: httpx.Timeout
) -> openai.AsyncOpenAI:
    """An ``AsyncOpenAI`` for ``provider`` on the caller's httpx client, without SDK retries.

    openai >= 3 is typed against ``httpx2`` but accepts a legacy ``httpx.AsyncClient`` at runtime; the
    platform builds its clients with httpx (``va_common.http``), so the client crosses that typing boundary
    here only.
    """
    legacy_client: Any = http
    return openai.AsyncOpenAI(
        base_url=provider.base_url,
        api_key=provider.api_key.get_secret_value(),
        timeout=sdk_timeout(timeout),
        max_retries=0,
        http_client=legacy_client,
    )


def _fingerprint(provider: LlmProvider) -> tuple[str, str]:
    key_hash = hashlib.sha256(provider.api_key.get_secret_value().encode()).hexdigest()
    return provider.base_url, key_hash


class LlmClients:
    """``AsyncOpenAI`` clients and per-provider concurrency limits (``max_concurrency``)."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
        self._clients: dict[str, tuple[tuple[str, str], openai.AsyncOpenAI]] = {}
        self._limits: dict[str, tuple[int, asyncio.Semaphore]] = {}

    def client(self, provider: LlmProvider) -> openai.AsyncOpenAI:
        """The cached client of ``provider`` (rebuilt when its base URL or key changed)."""
        fingerprint = _fingerprint(provider)
        cached = self._clients.get(provider.id)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
        client = openai_client(provider, self._http, timeout=CLIENT_TIMEOUT)
        self._clients[provider.id] = (fingerprint, client)
        return client

    def limit(self, provider: LlmProvider) -> asyncio.Semaphore:
        """The semaphore bounding concurrent requests to ``provider``."""
        cached = self._limits.get(provider.id)
        if cached is not None and cached[0] == provider.max_concurrency:
            return cached[1]
        semaphore = asyncio.Semaphore(provider.max_concurrency)
        self._limits[provider.id] = (provider.max_concurrency, semaphore)
        return semaphore

    async def prewarm(self, provider: LlmProvider) -> bool:
        """Open a connection with one ``GET {base_url}/models``; a failure is logged, never raised."""
        try:
            await self.client(provider).models.list(timeout=sdk_timeout(_PREWARM_TIMEOUT))
        except openai.OpenAIError as exc:
            _log.warning("llm.prewarm_failed", provider=provider.id, error=type(exc).__name__)
            return False
        return True
