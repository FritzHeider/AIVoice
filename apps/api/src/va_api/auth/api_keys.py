"""Minting API keys and naming who created them (specs/04-auth-tenancy-rbac.md §2.1, §8.2, §9).

A minted key exists in plaintext only in :class:`MintedKey` until it is returned to the caller once; the
database stores its prefix and peppered hash. Sub-account creation and the ``/v1/api_keys`` endpoints both
mint here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from va_api.auth.context import RequestContext
from va_common.auth import ApiKeyHasher, KeyEnvironment, api_key_prefix, generate_api_key
from va_db.repositories import ApiKeyRepo
from va_db.session import use_org

__all__ = ["USER_PRINCIPALS", "MintedKey", "creating_user_id", "mint_api_key"]

USER_PRINCIPALS: Final = frozenset({"user", "platform_admin"})


@dataclass(frozen=True, slots=True)
class MintedKey:
    """A fresh key: the plaintext (never in ``repr``), its prefix, its hash and the pepper version used."""

    plaintext: str = field(repr=False)
    prefix: str
    secret_hash: str
    pepper_version: int


def mint_api_key(hasher: ApiKeyHasher, environment: KeyEnvironment) -> MintedKey:
    """A new ``va_<environment>_…`` key hashed with the current pepper."""
    plaintext = generate_api_key(environment)
    return MintedKey(
        plaintext=plaintext,
        prefix=api_key_prefix(plaintext),
        secret_hash=hasher.hash(plaintext),
        pepper_version=hasher.current_version,
    )


async def creating_user_id(ctx: RequestContext, session: AsyncSession) -> uuid.UUID | None:
    """The session's user, or the user who created the calling API key (``None`` when it has none).

    ``session`` is a ``tenant_session(ctx.org_id)``; the key row is read in the org owning the key, which is
    the parent while acting on a sub-account (specs/04 §8.3, RLS switched for the lookup only).
    """
    if ctx.principal_type in USER_PRINCIPALS:
        return ctx.principal_id
    if ctx.principal_type == "api_key":
        async with use_org(session, ctx.principal_org_id):
            key = await ApiKeyRepo(session, ctx.principal_org_id).get(ctx.principal_id)
        return key.created_by if key is not None else None
    return None
