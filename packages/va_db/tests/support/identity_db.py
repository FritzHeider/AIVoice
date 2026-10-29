"""A migrated :class:`Database` with a local-KMS :class:`Envelope` and an :class:`OrgProvisioner`."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import func, select
from tenant_rows import open_database

from va_common.crypto import Envelope, LocalKmsProvider
from va_db.models import Membership, Org, OrgDataKey, User
from va_db.provisioning import OrgProvisioner
from va_db.repositories import OrgDataKeyStore
from va_db.session import Database


@dataclass(frozen=True, slots=True)
class IdentityDb:
    database: Database
    envelope: Envelope
    provisioner: OrgProvisioner


@asynccontextmanager
async def open_identity_db(url: str) -> AsyncIterator[IdentityDb]:
    """:func:`open_database` plus an envelope over a fresh random local master key."""
    async with open_database(url) as database:
        envelope = Envelope(LocalKmsProvider.from_hex(secrets.token_hex(32)), OrgDataKeyStore(database))
        yield IdentityDb(database, envelope, OrgProvisioner(database, envelope))


@dataclass(frozen=True, slots=True)
class RowCounts:
    orgs: int
    data_keys: int
    memberships: int
    users: int


async def row_counts(database: Database) -> RowCounts:
    """Total rows of the tables provisioning writes, read through the system session."""
    async with database.system_session() as session:
        counts = [
            (await session.execute(select(func.count()).select_from(model))).scalar_one()
            for model in (Org, OrgDataKey, Membership, User)
        ]
    return RowCounts(*counts)


def unique_email(label: str = "user") -> str:
    return f"{label}-{secrets.token_hex(4)}@example.org"
