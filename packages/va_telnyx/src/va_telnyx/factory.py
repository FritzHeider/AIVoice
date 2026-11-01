"""``TelnyxClientFactory``: the platform client or a BYO account's client (specs/06-telephony-telnyx.md §1).

A BYO API key comes from the injected resolver (envelope-decrypting ``telnyx_accounts.api_key_enc``) and is
cached in process for 5 minutes per account; concurrent lookups of one account share one resolve. A resolver
answering ``None`` (unknown or disabled account) raises :class:`UnknownTelnyxAccount`. Key rotation or account
deletion calls :meth:`TelnyxClientFactory.invalidate`.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

import httpx
from opentelemetry.trace import TracerProvider

from va_common.time import Clock, SystemClock
from va_telnyx.client import PLATFORM_ACCOUNT, TelnyxClient
from va_telnyx.metrics import TelnyxMetrics
from va_telnyx.rate_limit import TelnyxRateLimiter
from va_telnyx.retry import RetryPolicy

__all__ = ["KEY_TTL_S", "HasCallConfig", "KeyResolver", "TelnyxClientFactory", "UnknownTelnyxAccount"]

KEY_TTL_S: Final = 300.0

type KeyResolver = Callable[[uuid.UUID], Awaitable[str | None]]


class UnknownTelnyxAccount(LookupError):  # noqa: N818 - names the missing thing, like LookupError subclasses
    """No usable API key for the Telnyx account (absent, disabled, or deleted)."""

    def __init__(self, telnyx_account_id: uuid.UUID) -> None:
        super().__init__(f"Telnyx account {telnyx_account_id} has no usable API key")
        self.telnyx_account_id = telnyx_account_id


class HasCallConfig(Protocol):
    """A call row (or its view) with the ``calls.config`` JSON."""

    @property
    def config(self) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class _Cached:
    client: TelnyxClient
    expires_at: float


class TelnyxClientFactory:
    """Builds the platform :class:`TelnyxClient` once and BYO clients on demand; ``repr`` hides keys."""

    def __init__(  # noqa: PLR0913 - the client collaborators plus the BYO key source
        self,
        *,
        base_url: str,
        http: httpx.AsyncClient,
        limiter: TelnyxRateLimiter,
        platform_api_key: str,
        resolve_key: KeyResolver,
        metrics: TelnyxMetrics | None = None,
        clock: Clock | None = None,
        rng: random.Random | None = None,
        retry: RetryPolicy | None = None,
        tracer_provider: TracerProvider | None = None,
        key_ttl_s: float = KEY_TTL_S,
    ) -> None:
        self._base_url = base_url
        self._http = http
        self._limiter = limiter
        self._resolve_key = resolve_key
        self._metrics = metrics
        self._clock = clock if clock is not None else SystemClock()
        self._rng = rng
        self._retry = retry
        self._tracer_provider = tracer_provider
        self._key_ttl_s = key_ttl_s
        self._cache: dict[uuid.UUID, _Cached] = {}
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}
        self._platform = self._client(platform_api_key, PLATFORM_ACCOUNT)

    def __repr__(self) -> str:
        return f"TelnyxClientFactory(base_url={self._base_url!r}, cached_accounts={len(self._cache)})"

    @property
    def platform(self) -> TelnyxClient:
        return self._platform

    async def for_call(self, call: HasCallConfig) -> TelnyxClient:
        """The client for ``calls.config.telnyx_account_id`` (absent or ``None`` = the platform account)."""
        raw = call.config.get("telnyx_account_id")
        if raw is None:
            return self._platform
        try:
            account_id = raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw))
        except ValueError as exc:
            msg = f"calls.config.telnyx_account_id is not a UUID: {raw!r}"
            raise ValueError(msg) from exc
        return await self.for_account(account_id)

    async def for_account(self, telnyx_account_id: uuid.UUID | None) -> TelnyxClient:
        """The platform client for ``None``, else the BYO client (key cached for ``key_ttl_s``)."""
        if telnyx_account_id is None:
            return self._platform
        cached = self._fresh(telnyx_account_id)
        if cached is not None:
            return cached
        lock = self._locks.setdefault(telnyx_account_id, asyncio.Lock())
        async with lock:
            cached = self._fresh(telnyx_account_id)
            if cached is not None:
                return cached
            api_key = await self._resolve_key(telnyx_account_id)
            if api_key is None:
                raise UnknownTelnyxAccount(telnyx_account_id)
            client = self._client(api_key, str(telnyx_account_id))
            self._cache[telnyx_account_id] = _Cached(client, self._clock.monotonic() + self._key_ttl_s)
            return client

    def invalidate(self, telnyx_account_id: uuid.UUID) -> None:
        """Forget the cached key of ``telnyx_account_id`` (the next lookup resolves it again)."""
        self._cache.pop(telnyx_account_id, None)

    def _fresh(self, telnyx_account_id: uuid.UUID) -> TelnyxClient | None:
        cached = self._cache.get(telnyx_account_id)
        if cached is None:
            return None
        if self._clock.monotonic() >= cached.expires_at:
            del self._cache[telnyx_account_id]
            return None
        return cached.client

    def _client(self, api_key: str, account: str) -> TelnyxClient:
        return TelnyxClient(
            api_key,
            self._base_url,
            self._http,
            self._limiter,
            account=account,
            metrics=self._metrics,
            clock=self._clock,
            rng=self._rng,
            retry=self._retry,
            tracer_provider=self._tracer_provider,
        )
