"""Per-org providers from ``org_llm_providers`` (specs/10-llm-gateway.md §1; Decision P03-T13a rule 3).

The row is read in the org's ``tenant_session`` (RLS) and its ``api_key_enc`` decrypted with the AAD
``org_llm_providers.api_key_enc:<row id>`` (specs/28 §2). Hits and misses are cached for ``ttl_s`` on the
injected clock; whoever changes the row calls :meth:`OrgProviderStore.invalidate` after the commit.
A lookup that fails (database or KMS unavailable, a key that does not decrypt, a stored ``models`` map that
does not name every role) raises :class:`va_llm.errors.OrgProviderError` and is not cached.
"""

from __future__ import annotations

import uuid
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Final, Protocol

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import SecretStr, TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from va_common.crypto import CryptoError, Envelope, column_aad
from va_common.time import Clock
from va_db.models import OrgLlmProvider
from va_llm.errors import OrgProviderError
from va_llm.providers import LlmProvider, ModelRole, org_provider_id

__all__ = ["OrgProviderStore", "TenantSessions", "api_key_aad", "encrypt_org_api_key"]


_MODELS: Final = TypeAdapter(dict[ModelRole, str])
# Everything a lookup may raise for an unavailable database/KMS or a bad stored row (OSError covers
# connection refusals and timeouts that the drivers do not wrap).
_LOOKUP_ERRORS: Final = (SQLAlchemyError, OSError, CryptoError, ValidationError, ClientError, BotoCoreError)


class TenantSessions(Protocol):
    """The part of :class:`va_db.session.Database` the store needs."""

    def tenant_session(self, org_id: uuid.UUID) -> AbstractAsyncContextManager[AsyncSession]: ...


def api_key_aad(row_id: uuid.UUID) -> str:
    """The associated data of ``org_llm_providers.api_key_enc`` for row ``row_id``."""
    return column_aad("org_llm_providers", "api_key_enc", row_id)


async def encrypt_org_api_key(
    envelope: Envelope, org_id: uuid.UUID, row_id: uuid.UUID, api_key: str
) -> bytes:
    """``api_key`` sealed for the ``api_key_enc`` column of row ``row_id``."""
    return await envelope.encrypt_str(org_id, api_key, api_key_aad(row_id))


@dataclass(frozen=True, slots=True)
class _Row:
    id: uuid.UUID
    base_url: str
    api_key_enc: bytes
    models: object
    supports_tool_choice: bool


@dataclass(frozen=True, slots=True)
class _Cached:
    provider: LlmProvider | None
    expires_at: float


class OrgProviderStore:
    """Org providers with a ``ttl_s`` cache (implements :class:`va_llm.providers.OrgProviderSource`)."""

    def __init__(
        self, database: TenantSessions, envelope: Envelope, *, clock: Clock, ttl_s: float = 60.0
    ) -> None:
        self._database = database
        self._envelope = envelope
        self._clock = clock
        self._ttl_s = ttl_s
        self._cache: dict[uuid.UUID, _Cached] = {}

    async def get(self, org_id: uuid.UUID) -> LlmProvider | None:
        cached = self._cache.get(org_id)
        if cached is not None and self._clock.monotonic() < cached.expires_at:
            return cached.provider
        try:
            provider = await self._load(org_id)
        except _LOOKUP_ERRORS as exc:
            raise OrgProviderError(provider_id=org_provider_id(org_id), cause=type(exc).__name__) from exc
        self._cache[org_id] = _Cached(provider, self._clock.monotonic() + self._ttl_s)
        return provider

    def invalidate(self, org_id: uuid.UUID) -> None:
        self._cache.pop(org_id, None)

    async def _load(self, org_id: uuid.UUID) -> LlmProvider | None:
        async with self._database.tenant_session(org_id) as session:
            found = (
                await session.execute(select(OrgLlmProvider).where(OrgLlmProvider.org_id == org_id))
            ).scalar_one_or_none()
            if found is None:
                return None
            row = _Row(found.id, found.base_url, found.api_key_enc, found.models, found.supports_tool_choice)
        api_key = await self._envelope.decrypt_str(org_id, row.api_key_enc, api_key_aad(row.id))
        return LlmProvider(
            id=org_provider_id(org_id),
            base_url=row.base_url,
            api_key=SecretStr(api_key),
            models=_MODELS.validate_python(row.models),
            supports_tool_choice=row.supports_tool_choice,
        )
