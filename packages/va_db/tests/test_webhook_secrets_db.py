"""Signing secret view, active set, and rotation on Postgres (specs/19 §10 Decision P06-T04a; specs/28 §3)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from identity_db import IdentityDb, open_identity_db, unique_email

from va_common.crypto import DecryptionError
from va_common.errors import AppError
from va_db.models import Org
from va_db.provisioning import ProvisionedOrg, webhook_secret_aad
from va_db.repositories import NewUser, OrgRepo
from va_db.webhook_secrets import (
    ROTATION_OVERLAP,
    RotatedSecret,
    active_signing_secrets,
    previous_secret_aad,
    rotate_signing_secret,
    secret_info,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


async def _provision(db: IdentityDb) -> ProvisionedOrg:
    return await db.provisioner.provision(name="Hooks", owner=NewUser(email=unique_email("owner")))


async def _org(db: IdentityDb, org_id: uuid.UUID) -> Org:
    async with db.database.global_session() as session:
        org = await OrgRepo(session).get(org_id)
    assert org is not None
    return org


async def _rotate(db: IdentityDb, org_id: uuid.UUID, now: datetime = NOW) -> RotatedSecret:
    async with db.database.global_session() as session:
        return await rotate_signing_secret(session, db.envelope, org_id, now=now)


async def _active(db: IdentityDb, org_id: uuid.UUID, now: datetime = NOW) -> tuple[str, ...]:
    return (await active_signing_secrets(db.envelope, await _org(db, org_id), now=now)).secrets


async def test_a_fresh_org_signs_with_its_provisioned_secret(identity_db: IdentityDb) -> None:
    provisioned = await _provision(identity_db)
    org = await _org(identity_db, provisioned.org.id)
    active = await active_signing_secrets(identity_db.envelope, org, now=NOW)
    info = await secret_info(identity_db.envelope, org, now=NOW)
    assert active.secrets == (provisioned.webhook_signing_secret,)
    assert active.previous_expires_at is None
    assert provisioned.webhook_signing_secret not in repr(active)
    assert info.secret_last4 == provisioned.webhook_signing_secret[-4:]
    assert info.created_at == org.created_at
    assert (info.previous_secret_last4, info.previous_expires_at) == (None, None)


async def test_rotation_keeps_the_old_secret_signing_for_24_hours(identity_db: IdentityDb) -> None:
    provisioned = await _provision(identity_db)
    old = provisioned.webhook_signing_secret
    rotated = await _rotate(identity_db, provisioned.org.id)
    assert rotated.secret.startswith("whsec_")
    assert rotated.secret != old
    assert rotated.secret not in repr(rotated)
    assert (rotated.created_at, rotated.previous_expires_at) == (NOW, NOW + ROTATION_OVERLAP)
    assert rotated.before.secret_last4 == old[-4:]
    assert rotated.before.previous_secret_last4 is None
    assert (rotated.after.secret_last4, rotated.after.previous_secret_last4) == (
        rotated.secret[-4:],
        old[-4:],
    )
    assert rotated.after.previous_expires_at == NOW + ROTATION_OVERLAP
    org_id = provisioned.org.id
    assert await _active(identity_db, org_id) == (rotated.secret, old)
    assert await _active(identity_db, org_id, NOW + ROTATION_OVERLAP - timedelta(seconds=1)) == (
        rotated.secret,
        old,
    )
    assert await _active(identity_db, org_id, NOW + ROTATION_OVERLAP) == (rotated.secret,)
    org = await _org(identity_db, org_id)
    info = await secret_info(identity_db.envelope, org, now=NOW + ROTATION_OVERLAP)
    assert (info.secret_last4, info.created_at) == (rotated.secret[-4:], NOW)
    assert (info.previous_secret_last4, info.previous_expires_at) == (None, None)


async def test_each_blob_is_bound_to_its_own_column(identity_db: IdentityDb) -> None:
    provisioned = await _provision(identity_db)
    org_id = provisioned.org.id
    await _rotate(identity_db, org_id)
    org = await _org(identity_db, org_id)
    assert org.webhook_signing_secret_prev_enc is not None
    with pytest.raises(DecryptionError):
        await identity_db.envelope.decrypt(
            org_id, org.webhook_signing_secret_prev_enc, webhook_secret_aad(org_id)
        )
    with pytest.raises(DecryptionError):
        await identity_db.envelope.decrypt(
            org_id, org.webhook_signing_secret_enc, previous_secret_aad(org_id)
        )


async def test_a_second_rotation_drops_the_oldest_secret(identity_db: IdentityDb) -> None:
    provisioned = await _provision(identity_db)
    org_id = provisioned.org.id
    first = await _rotate(identity_db, org_id)
    later = NOW + timedelta(hours=1)
    second = await _rotate(identity_db, org_id, later)
    assert second.before.previous_secret_last4 == provisioned.webhook_signing_secret[-4:]
    assert await _active(identity_db, org_id, later) == (second.secret, first.secret)
    assert second.previous_expires_at == later + ROTATION_OVERLAP


async def test_rotating_an_unknown_org_is_not_found(identity_db: IdentityDb) -> None:
    with pytest.raises(AppError) as caught:
        await _rotate(identity_db, uuid.uuid4())
    assert caught.value.code == "NOT_FOUND"


async def test_concurrent_rotations_serialise_on_the_org_lock(migrated_url: str) -> None:
    async with open_identity_db(migrated_url) as db:
        provisioned = await _provision(db)
        org_id = provisioned.org.id
        first, second = await asyncio.gather(_rotate(db, org_id), _rotate(db, org_id))
        active = await _active(db, org_id)
    secrets = {first.secret: first, second.secret: second}
    winner = secrets[active[0]]
    loser = second if winner is first else first
    # The later rotation demoted the earlier rotation's secret, so no rotation was lost.
    assert active == (winner.secret, loser.secret)
    assert winner.before.secret_last4 == loser.secret[-4:]
    assert loser.before.secret_last4 == provisioned.webhook_signing_secret[-4:]
