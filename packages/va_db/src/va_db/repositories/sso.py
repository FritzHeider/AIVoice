"""SSO connections (specs/04-auth-tenancy-rbac.md §5; specs/03-data-model.md §2, §3 ``sso_connections``).

:class:`SsoConnectionRepo` manages one org's single connection on a ``tenant_session`` (RLS plus an explicit
``org_id`` filter). :class:`SsoConnectionLookupRepo` maps email domains to connections across orgs, which
happens before any org context exists (SSO start) and for the cross-org domain claim check, so it is a
``va_system`` repository named in specs/03 §2.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass

from sqlalchemy import Text, any_, delete, func, literal, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from va_db.models import MemberRole, SsoConnection
from va_db.repositories.base import SystemRepository, conflict_savepoint

__all__ = ["SsoConnectionLookupRepo", "SsoConnectionRepo", "SsoDomainClaim"]

_CONFLICT_MESSAGE = "An SSO connection for this issuer already exists."


@dataclass(frozen=True, slots=True)
class SsoDomainClaim:
    """``domain`` is listed in ``allowed_domains`` of connection ``connection_id`` of ``org_id``."""

    org_id: uuid.UUID
    connection_id: uuid.UUID
    domain: str


class SsoConnectionRepo:
    """The SSO connection of one org; use it inside ``tenant_session(org_id)`` with the same org."""

    def __init__(self, session: AsyncSession, org_id: uuid.UUID) -> None:
        self.session = session
        self.org_id = org_id

    async def get(self) -> SsoConnection | None:
        """The org's connection (the oldest, should there ever be several), or ``None``."""
        statement = (
            select(SsoConnection)
            .where(SsoConnection.org_id == self.org_id)
            .order_by(SsoConnection.created_at, SsoConnection.id)
            .limit(1)
        )
        return await self.session.scalar(statement)

    async def upsert(  # noqa: PLR0913 - keyword-only connection fields
        self,
        *,
        connection_id: uuid.UUID,
        issuer: str,
        client_id: str,
        client_secret_enc: bytes | None,
        allowed_domains: Sequence[str],
        default_role: MemberRole,
        enforce: bool,
    ) -> SsoConnection:
        """Update the org's connection in place, or create it with ``connection_id``.

        ``client_secret_enc=None`` keeps the stored secret; creating requires one (``ValueError``). The
        ``set_updated_at()`` trigger maintains ``updated_at``.
        """
        row = await self.get()
        if row is None and client_secret_enc is None:
            msg = "client_secret_enc is required when creating an SSO connection"
            raise ValueError(msg)
        async with conflict_savepoint(self.session, _CONFLICT_MESSAGE, field="issuer"):
            if row is None:
                row = SsoConnection(id=connection_id, org_id=self.org_id, provider="oidc")
                self.session.add(row)
            row.issuer = issuer
            row.client_id = client_id
            if client_secret_enc is not None:
                row.client_secret_enc = client_secret_enc
            row.allowed_domains = list(allowed_domains)
            row.default_role = default_role
            row.enforce = enforce
        await self.session.refresh(row)
        return row

    async def delete(self) -> uuid.UUID | None:
        """Remove the org's connection; its id, or ``None`` when there was none."""
        row = await self.get()
        if row is None:
            return None
        await self.session.execute(
            delete(SsoConnection).where(SsoConnection.org_id == self.org_id, SsoConnection.id == row.id)
        )
        return row.id


class SsoConnectionLookupRepo(SystemRepository):
    """Email domain → SSO connection across orgs (system scope, specs/03-data-model.md §2)."""

    async def claims(self, domains: Collection[str]) -> list[SsoDomainClaim]:
        """Each (connection, domain) pair where ``allowed_domains`` lists one of ``domains``, oldest first."""
        if not domains:
            return []
        wanted = literal(sorted(set(domains)), ARRAY(Text))
        expanded = select(
            SsoConnection.org_id,
            SsoConnection.id.label("connection_id"),
            SsoConnection.created_at,
            func.unnest(SsoConnection.allowed_domains).label("domain"),
        ).subquery()
        statement = (
            select(expanded.c.org_id, expanded.c.connection_id, expanded.c.domain)
            .where(expanded.c.domain == any_(wanted))
            .order_by(expanded.c.created_at, expanded.c.connection_id, expanded.c.domain)
        )
        rows = (await self.session.execute(statement)).all()
        return [
            SsoDomainClaim(org_id=org_id, connection_id=row_id, domain=name) for org_id, row_id, name in rows
        ]

    async def for_domain(self, domain: str) -> SsoConnection | None:
        """The oldest connection whose ``allowed_domains`` lists ``domain``, or ``None``."""
        statement = (
            select(SsoConnection)
            .where(literal(domain, Text) == any_(SsoConnection.allowed_domains))
            .order_by(SsoConnection.created_at, SsoConnection.id)
            .limit(1)
        )
        return await self.session.scalar(statement)
