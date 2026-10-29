"""``MfaRepo`` against a migrated database (specs/04-auth-tenancy-rbac.md §4.5; specs/03-data-model.md §3).

Runs on ``global_session()`` like the other identity repositories. The pool holds one connection per role, so
transactions run one after another, never nested.
"""

from __future__ import annotations

import os
import uuid

import pytest
from identity_db import IdentityDb, unique_email

from va_db.models import User
from va_db.repositories import IdentityRepo, MfaRepo, NewUser
from va_db.session import Database

pytestmark = pytest.mark.integration

_HASHES = [f"$argon2id$v=19$m=19456,t=2,p=1$c2FsdA${index:02d}" for index in range(3)]


async def _user(database: Database) -> uuid.UUID:
    async with database.global_session() as session:
        return (await IdentityRepo(session).create_user(NewUser(email=unique_email()))).id


async def _row(database: Database, user_id: uuid.UUID) -> User:
    async with database.global_session() as session:
        user = await IdentityRepo(session).get_user(user_id)
    assert user is not None
    return user


async def _enrolled(database: Database) -> tuple[uuid.UUID, bytes]:
    user_id, blob = await _user(database), os.urandom(48)
    async with database.global_session() as session:
        repo = MfaRepo(session)
        assert await repo.begin_setup(user_id, blob)
        assert await repo.enable(user_id, blob, list(_HASHES))
    return user_id, blob


async def test_begin_setup_stores_and_replaces_the_pending_secret(identity_db: IdentityDb) -> None:
    database = identity_db.database
    user_id = await _user(database)
    first, second = os.urandom(48), os.urandom(48)
    async with database.global_session() as session:
        assert await MfaRepo(session).begin_setup(user_id, first)
    async with database.global_session() as session:
        assert await MfaRepo(session).begin_setup(user_id, second)
    user = await _row(database, user_id)
    assert (user.mfa_totp_secret_enc, user.mfa_enabled, user.mfa_recovery_codes_hash) == (second, False, [])


async def test_begin_setup_refused_while_enabled(identity_db: IdentityDb) -> None:
    user_id, blob = await _enrolled(identity_db.database)
    async with identity_db.database.global_session() as session:
        assert not await MfaRepo(session).begin_setup(user_id, os.urandom(48))
    assert (await _row(identity_db.database, user_id)).mfa_totp_secret_enc == blob


async def test_enable_needs_the_verified_pending_secret(identity_db: IdentityDb) -> None:
    database = identity_db.database
    user_id = await _user(database)
    verified, replaced = os.urandom(48), os.urandom(48)
    async with database.global_session() as session:
        repo = MfaRepo(session)
        assert not await repo.enable(user_id, verified, list(_HASHES))
        assert await repo.begin_setup(user_id, verified)
        assert await repo.begin_setup(user_id, replaced)
        assert not await repo.enable(user_id, verified, list(_HASHES))
    assert not (await _row(database, user_id)).mfa_enabled
    async with database.global_session() as session:
        repo = MfaRepo(session)
        assert await repo.enable(user_id, replaced, list(_HASHES))
        assert not await repo.enable(user_id, replaced, list(_HASHES))
    user = await _row(database, user_id)
    assert (user.mfa_enabled, user.mfa_totp_secret_enc, user.mfa_recovery_codes_hash) == (
        True,
        replaced,
        _HASHES,
    )


async def test_disable_clears_every_mfa_column(identity_db: IdentityDb) -> None:
    database = identity_db.database
    user_id, _ = await _enrolled(database)
    async with database.global_session() as session:
        assert await MfaRepo(session).disable(user_id)
    user = await _row(database, user_id)
    assert (user.mfa_enabled, user.mfa_totp_secret_enc, user.mfa_recovery_codes_hash) == (False, None, [])
    async with database.global_session() as session:
        assert not await MfaRepo(session).disable(user_id)


async def test_recovery_code_is_consumed_once(identity_db: IdentityDb) -> None:
    database = identity_db.database
    user_id, _ = await _enrolled(database)
    async with database.global_session() as session:
        assert await MfaRepo(session).consume_recovery_code(user_id, _HASHES[1])
    async with database.global_session() as session:
        assert not await MfaRepo(session).consume_recovery_code(user_id, _HASHES[1])
    assert (await _row(database, user_id)).mfa_recovery_codes_hash == [_HASHES[0], _HASHES[2]]
    async with database.global_session() as session:
        assert not await MfaRepo(session).consume_recovery_code(user_id, "$argon2id$unknown")


async def test_recovery_code_refused_when_mfa_disabled(identity_db: IdentityDb) -> None:
    database = identity_db.database
    user_id, blob = await _enrolled(database)
    other_id, _ = await _enrolled(database)
    async with database.global_session() as session:
        assert await MfaRepo(session).disable(user_id)
        assert await MfaRepo(session).begin_setup(user_id, blob)
    async with database.global_session() as session:
        assert not await MfaRepo(session).consume_recovery_code(user_id, _HASHES[0])
        assert await MfaRepo(session).consume_recovery_code(other_id, _HASHES[0])
    assert (await _row(database, other_id)).mfa_recovery_codes_hash == _HASHES[1:]
