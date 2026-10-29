"""Atomic org provisioning (specs/04-auth-tenancy-rbac.md §4.1; specs/28-security.md §3).

One ``tenant_session(new_org_id)`` transaction creates the org row with a unique slug, its active data key,
its envelope-encrypted webhook signing secret, and optionally the owner user plus an ``owner`` membership.
Signup, ``POST /v1/orgs/create`` and dev seeding all go through :class:`OrgProvisioner`, so an org never
exists without its data key or secret.
"""

from __future__ import annotations

import re
import secrets
import string
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from va_common.auth import generate_token
from va_common.crypto import Envelope, column_aad
from va_common.errors import AppError
from va_common.ids import new_id
from va_db.models import Membership, Org, User
from va_db.repositories.base import NotFoundError, conflict_savepoint
from va_db.repositories.data_keys import OrgDataKeyRepo
from va_db.repositories.identity import IdentityRepo, NewUser
from va_db.repositories.memberships import MembershipRepo
from va_db.session import Database

__all__ = [
    "SLUG_ATTEMPTS",
    "SLUG_MAX_BASE",
    "SLUG_SUFFIX_LEN",
    "WEBHOOK_SECRET_PREFIX",
    "OrgProvisioner",
    "ProvisionedOrg",
    "make_slug",
    "random_slug_suffix",
    "slug_base",
    "webhook_secret_aad",
]

SLUG_MAX_BASE: Final = 40
SLUG_SUFFIX_LEN: Final = 4
SLUG_ATTEMPTS: Final = 5
SLUG_FALLBACK: Final = "org"
WEBHOOK_SECRET_PREFIX: Final = "whsec_"  # noqa: S105 - public prefix, not a secret
_SLUG_ALPHABET: Final = string.ascii_lowercase + string.digits
_NON_SLUG_RUN: Final = re.compile(r"[^a-z0-9]+")


def slug_base(name: str) -> str:
    """``name`` NFKD-folded to ASCII, lower-cased, non-``[a-z0-9]`` runs as ``-``, trimmed, at most 40 chars.

    ``org`` when nothing is left (for example a name in a non-Latin script).
    """
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    base = _NON_SLUG_RUN.sub("-", folded).strip("-")[:SLUG_MAX_BASE].strip("-")
    return base or SLUG_FALLBACK


def random_slug_suffix() -> str:
    """Four characters from ``[a-z0-9]`` drawn with ``secrets``."""
    return "".join(secrets.choice(_SLUG_ALPHABET) for _ in range(SLUG_SUFFIX_LEN))


def make_slug(name: str, *, suffix: str | None = None) -> str:
    """``slug_base(name)`` + ``-`` + ``suffix`` (a fresh :func:`random_slug_suffix` when ``None``)."""
    return f"{slug_base(name)}-{random_slug_suffix() if suffix is None else suffix}"


def webhook_secret_aad(org_id: uuid.UUID) -> str:
    """The envelope AAD of ``orgs.webhook_signing_secret_enc`` for ``org_id``."""
    return column_aad("orgs", "webhook_signing_secret_enc", org_id)


@dataclass(frozen=True, slots=True)
class ProvisionedOrg:
    """What provisioning created; ``webhook_signing_secret`` is plaintext and never appears in ``repr``."""

    org: Org
    owner: User | None
    membership: Membership | None
    webhook_signing_secret: str = field(repr=False)


class OrgProvisioner:
    """Creates orgs with their data key, encrypted webhook secret, and optional owner in one transaction."""

    def __init__(self, database: Database, envelope: Envelope) -> None:
        self._database = database
        self._envelope = envelope

    async def provision(
        self,
        *,
        name: str,
        owner: uuid.UUID | NewUser | None,
        plan_id: str = "start",
        parent_org_id: uuid.UUID | None = None,
        kyc_level: int = 0,
    ) -> ProvisionedOrg:
        """Provision a new org in its own ``tenant_session``; nothing persists when any step fails."""
        org_id = new_id()
        async with self._database.tenant_session(org_id) as session:
            return await self.provision_in(
                session,
                org_id,
                name=name,
                owner=owner,
                plan_id=plan_id,
                parent_org_id=parent_org_id,
                kyc_level=kyc_level,
            )

    async def provision_in(  # noqa: PLR0913 - keyword-only org attributes
        self,
        session: AsyncSession,
        org_id: uuid.UUID,
        *,
        name: str,
        owner: uuid.UUID | NewUser | None,
        plan_id: str = "start",
        parent_org_id: uuid.UUID | None = None,
        kyc_level: int = 0,
    ) -> ProvisionedOrg:
        """Provision org ``org_id`` inside the caller's ``session``, which must be ``tenant_session(org_id)``.

        The caller owns the transaction, so it can write more rows (a login session, a verification token)
        that commit or roll back together with the org. ``INVALID_INPUT`` for an empty name, ``CONFLICT`` for
        a duplicate owner email or exhausted slug attempts, ``NOT_FOUND`` for an unknown owner id.
        """
        org_name = name.strip()
        if not org_name:
            raise AppError("INVALID_INPUT", "Org name must not be empty.", field="org_name")
        wrapped = await self._envelope.create_data_key(org_id)
        secret = WEBHOOK_SECRET_PREFIX + generate_token()
        secret_enc = await self._envelope.encrypt_with_key(
            org_id, wrapped, secret.encode(), webhook_secret_aad(org_id)
        )
        org = await self._insert_org(
            session,
            name=org_name,
            build=lambda slug: Org(
                id=org_id,
                name=org_name,
                slug=slug,
                plan_id=plan_id,
                parent_org_id=parent_org_id,
                kyc_level=kyc_level,
                webhook_signing_secret_enc=secret_enc,
            ),
        )
        await OrgDataKeyRepo(session).add_active(org_id, wrapped)
        if owner is None:
            return ProvisionedOrg(org=org, owner=None, membership=None, webhook_signing_secret=secret)
        user = await self._owner(session, owner)
        membership = await MembershipRepo(session, org_id).add(user.id, "owner")
        return ProvisionedOrg(org=org, owner=user, membership=membership, webhook_signing_secret=secret)

    async def webhook_signing_secret(self, org: Org) -> str:
        """Decrypt the org's webhook signing secret."""
        return await self._envelope.decrypt_str(
            org.id, org.webhook_signing_secret_enc, webhook_secret_aad(org.id)
        )

    async def _insert_org(self, session: AsyncSession, *, name: str, build: Callable[[str], Org]) -> Org:
        """Insert ``build(slug)`` with a fresh slug per attempt, at most ``SLUG_ATTEMPTS`` times."""
        for _ in range(SLUG_ATTEMPTS):
            org = build(make_slug(name))
            try:
                async with conflict_savepoint(session, "Org slug is taken."):
                    session.add(org)
            except AppError:
                continue
            await session.refresh(org)
            return org
        raise AppError("CONFLICT", "Could not allocate a unique org slug.")

    async def _owner(self, session: AsyncSession, owner: uuid.UUID | NewUser) -> User:
        identity = IdentityRepo(session)
        if isinstance(owner, NewUser):
            return await identity.create_user(owner)
        user = await identity.get_user(owner)
        if user is None:
            raise NotFoundError("user")
        return user
