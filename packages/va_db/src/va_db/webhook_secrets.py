"""Org webhook signing secrets: view, active set, and rotation (specs/19 §10 Decision P06-T04a; specs/28 §3).

The current secret is ``orgs.webhook_signing_secret_enc``. Rotating keeps exactly one previous secret
in ``webhook_signing_secret_prev_enc`` for :data:`ROTATION_OVERLAP`; until then every delivery is signed with
both (current first). A rotation inside an open window replaces the older previous secret. Every timestamp
comes from the caller's clock, never the database's ``now()``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from va_common.auth import generate_token
from va_common.crypto import CryptoError, Envelope, column_aad
from va_db.models import Org
from va_db.provisioning import WEBHOOK_SECRET_PREFIX, webhook_secret_aad
from va_db.repositories.base import NotFoundError
from va_db.repositories.data_keys import OrgDataKeyRepo
from va_db.repositories.orgs import OrgRepo
from va_db.session import use_org

__all__ = [
    "ROTATION_OVERLAP",
    "ActiveSecrets",
    "RotatedSecret",
    "SecretInfo",
    "active_signing_secrets",
    "previous_secret_aad",
    "rotate_signing_secret",
    "secret_info",
]

ROTATION_OVERLAP: Final = timedelta(hours=24)
_LAST_CHARS: Final = 4


def previous_secret_aad(org_id: uuid.UUID) -> str:
    """The envelope AAD of ``orgs.webhook_signing_secret_prev_enc`` for ``org_id``."""
    return column_aad("orgs", "webhook_signing_secret_prev_enc", org_id)


@dataclass(frozen=True)
class SecretInfo:
    """What may be shown about the org's secrets: last four characters and timestamps only."""

    secret_last4: str
    created_at: datetime
    previous_secret_last4: str | None
    previous_expires_at: datetime | None


@dataclass(frozen=True)
class ActiveSecrets:
    """The secrets to sign with, current first; ``previous_expires_at`` is set while a previous one signs."""

    secrets: tuple[str, ...] = field(repr=False)
    previous_expires_at: datetime | None


@dataclass(frozen=True)
class RotatedSecret:
    """A rotation's outcome; ``secret`` is the new plaintext, shown to the caller once."""

    secret: str = field(repr=False)
    created_at: datetime
    previous_expires_at: datetime
    before: SecretInfo
    after: SecretInfo


def _previous_open(org: Org, now: datetime) -> bool:
    expires = org.webhook_signing_secret_prev_expires_at
    return org.webhook_signing_secret_prev_enc is not None and expires is not None and now < expires


async def _decrypted(envelope: Envelope, org: Org, now: datetime) -> tuple[str, str | None]:
    current = await envelope.decrypt_str(org.id, org.webhook_signing_secret_enc, webhook_secret_aad(org.id))
    previous_enc = org.webhook_signing_secret_prev_enc
    if previous_enc is None or not _previous_open(org, now):
        return current, None
    return current, await envelope.decrypt_str(org.id, previous_enc, previous_secret_aad(org.id))


def _info(org: Org, current: str, previous: str | None) -> SecretInfo:
    return SecretInfo(
        secret_last4=current[-_LAST_CHARS:],
        created_at=org.webhook_signing_secret_created_at,
        previous_secret_last4=None if previous is None else previous[-_LAST_CHARS:],
        previous_expires_at=None if previous is None else org.webhook_signing_secret_prev_expires_at,
    )


async def active_signing_secrets(envelope: Envelope, org: Org, *, now: datetime) -> ActiveSecrets:
    """The secrets of ``org`` to sign with at ``now``: current, plus the previous one while it is open."""
    current, previous = await _decrypted(envelope, org, now)
    if previous is None:
        return ActiveSecrets(secrets=(current,), previous_expires_at=None)
    return ActiveSecrets(
        secrets=(current, previous), previous_expires_at=org.webhook_signing_secret_prev_expires_at
    )


async def secret_info(envelope: Envelope, org: Org, *, now: datetime) -> SecretInfo:
    """The displayable view of the secrets of ``org`` at ``now``."""
    current, previous = await _decrypted(envelope, org, now)
    return _info(org, current, previous)


async def rotate_signing_secret(
    session: AsyncSession, envelope: Envelope, org_id: uuid.UUID, *, now: datetime
) -> RotatedSecret:
    """Issue a new secret for ``org_id`` and keep the current one valid for :data:`ROTATION_OVERLAP`.

    ``session`` is a ``global_session()``; the org row is locked first, so concurrent rotations serialise and
    each one demotes the secret the previous rotation issued. Both blobs are sealed with the org's active data
    key, read in this transaction. ``NOT_FOUND`` for an unknown org.
    """
    org = await OrgRepo(session).lock(org_id)
    if org is None:
        raise NotFoundError("Org")
    async with use_org(session, org_id):
        wrapped = await OrgDataKeyRepo(session).active(org_id)
    if wrapped is None:
        msg = f"no active data key for org {org_id}"
        raise CryptoError(msg)
    current, previous = await _decrypted(envelope, org, now)
    before = _info(org, current, previous)
    secret = WEBHOOK_SECRET_PREFIX + generate_token()
    expires = now + ROTATION_OVERLAP
    org.webhook_signing_secret_enc = await envelope.encrypt_with_key(
        org_id, wrapped, secret.encode(), webhook_secret_aad(org_id)
    )
    org.webhook_signing_secret_prev_enc = await envelope.encrypt_with_key(
        org_id, wrapped, current.encode(), previous_secret_aad(org_id)
    )
    org.webhook_signing_secret_created_at = now
    org.webhook_signing_secret_prev_expires_at = expires
    await session.flush()
    return RotatedSecret(
        secret=secret,
        created_at=now,
        previous_expires_at=expires,
        before=before,
        after=_info(org, secret, current),
    )
