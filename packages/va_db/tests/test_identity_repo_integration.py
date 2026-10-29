"""``IdentityRepo`` against a migrated database (specs/04-auth-tenancy-rbac.md §4.1, §4.3; specs/03 §2).

Runs on ``global_session()``: the identity tables have no RLS, and only token hashes are ever stored.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address

import pytest
from identity_db import IdentityDb, unique_email
from model_factories import make_org
from sqlalchemy import select, text

from va_common.auth import sha256_hex
from va_common.errors import AppError
from va_db.models import EmailToken, Session, User
from va_db.repositories import EMAIL_VERIFY_TTL, PASSWORD_RESET_TTL, IdentityRepo, NewUser
from va_db.session import Database

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
_HASH = "$argon2id$v=19$m=19456,t=2,p=1$c2FsdA$aGFzaA"


async def _user(database: Database, email: str | None = None) -> User:
    async with database.global_session() as session:
        return await IdentityRepo(session).create_user(NewUser(email=email or unique_email()))


async def _new_session(
    database: Database, user_id: uuid.UUID, *, expires_in: timedelta
) -> tuple[str, Session]:
    async with database.global_session() as session:
        issued = await IdentityRepo(session).create_session(
            user_id=user_id,
            active_org_id=None,
            expires_at=_NOW + expires_in,
            ip="203.0.113.7",
            user_agent="ua",
        )
    return issued.token, issued.session


async def test_ttl_constants_match_spec() -> None:
    assert timedelta(hours=24) == EMAIL_VERIFY_TTL
    assert timedelta(hours=1) == PASSWORD_RESET_TTL


async def test_create_user_and_lookup_case_insensitive(identity_db: IdentityDb) -> None:
    email = unique_email("Jane").replace("@example.org", "@Example.org")
    async with identity_db.database.global_session() as session:
        repo = IdentityRepo(session)
        user = await repo.create_user(NewUser(email=f"  {email} ", name="Jane", password_hash=_HASH))
        found = await repo.get_user_by_email(email.upper())
        assert await repo.get_user(user.id) is user
        assert await repo.get_user(uuid.uuid4()) is None
        assert await repo.get_user_by_email(unique_email()) is None
    assert found is not None
    assert found.id == user.id
    assert user.email == email
    assert (user.name, user.password_hash, user.email_verified_at, user.status) == (
        "Jane",
        _HASH,
        None,
        "active",
    )
    assert _HASH not in repr(NewUser(email=email, password_hash=_HASH))


async def test_create_user_duplicate_email_conflict(identity_db: IdentityDb) -> None:
    email = unique_email()
    await _user(identity_db.database, email)
    async with identity_db.database.global_session() as session:
        repo = IdentityRepo(session)
        with pytest.raises(AppError) as info:
            await repo.create_user(NewUser(email=email.upper()))
        assert (info.value.code, info.value.field) == ("CONFLICT", "email")
        assert info.value.message == "An account with this email already exists."
        other = await repo.create_user(NewUser(email=unique_email()))
    async with identity_db.database.global_session() as session:
        assert await IdentityRepo(session).get_user(other.id) is not None


async def test_mark_email_verified_keeps_first_timestamp(identity_db: IdentityDb) -> None:
    user = await _user(identity_db.database)
    async with identity_db.database.global_session() as session:
        repo = IdentityRepo(session)
        await repo.mark_email_verified(user.id, _NOW)
        await repo.mark_email_verified(user.id, _NOW + timedelta(hours=1))
        await repo.set_password_hash(user.id, "$argon2id$new")
        await repo.record_login(user.id, _NOW + timedelta(minutes=5))
    async with identity_db.database.global_session() as session:
        row = (await session.execute(select(User).where(User.id == user.id))).scalar_one()
    assert (row.email_verified_at, row.password_hash, row.last_login_at) == (
        _NOW,
        "$argon2id$new",
        _NOW + timedelta(minutes=5),
    )


async def test_session_stores_only_hash(identity_db: IdentityDb) -> None:
    user = await _user(identity_db.database)
    token, row = await _new_session(identity_db.database, user.id, expires_in=timedelta(days=1))
    assert row.token_hash == sha256_hex(token)
    assert row.ip == ip_address("203.0.113.7")
    assert (row.user_agent, row.mfa_passed, row.revoked_at) == ("ua", False, None)
    async with identity_db.database.global_session() as session:
        text_columns = (
            await session.execute(
                text(
                    "SELECT token_hash, coalesce(user_agent, ''), coalesce(host(ip), '') "
                    "FROM sessions WHERE id = :i"
                ),
                {"i": row.id},
            )
        ).one()
    assert all(token not in value for value in text_columns)


async def test_create_session_rejects_bad_ip(identity_db: IdentityDb) -> None:
    user = await _user(identity_db.database)
    async with identity_db.database.global_session() as session:
        with pytest.raises(ValueError, match="does not appear to be an IPv4 or IPv6 address"):
            await IdentityRepo(session).create_session(
                user_id=user.id, active_org_id=None, expires_at=_NOW, ip="not-an-ip", user_agent=None
            )


async def test_get_active_session_excludes_expired_and_revoked(identity_db: IdentityDb) -> None:
    user = await _user(identity_db.database)
    live_token, live = await _new_session(identity_db.database, user.id, expires_in=timedelta(hours=1))
    expired_token, _ = await _new_session(identity_db.database, user.id, expires_in=-timedelta(seconds=1))
    revoked_token, revoked = await _new_session(identity_db.database, user.id, expires_in=timedelta(hours=1))
    async with identity_db.database.global_session() as session:
        repo = IdentityRepo(session)
        assert await repo.revoke_session(revoked.id, _NOW)
        assert not await repo.revoke_session(revoked.id, _NOW)
        found = await repo.get_active_session(live_token, _NOW)
        assert await repo.get_active_session(expired_token, _NOW) is None
        assert await repo.get_active_session(revoked_token, _NOW) is None
        assert await repo.get_active_session("unknown-token", _NOW) is None
    assert found is not None
    assert found.id == live.id


async def test_extend_and_set_active_org_and_mfa(identity_db: IdentityDb) -> None:
    user = await _user(identity_db.database)
    token, row = await _new_session(identity_db.database, user.id, expires_in=timedelta(minutes=1))
    async with identity_db.database.global_session() as session:
        org = await make_org(session)
        repo = IdentityRepo(session)
        await repo.extend_session(row.id, _NOW + timedelta(days=7))
        await repo.set_active_org(row.id, org.id)
        await repo.set_mfa_passed(row.id)
    async with identity_db.database.global_session() as session:
        found = await IdentityRepo(session).get_active_session(token, _NOW + timedelta(days=6))
    assert found is not None
    assert (found.expires_at, found.active_org_id, found.mfa_passed) == (
        _NOW + timedelta(days=7),
        org.id,
        True,
    )


async def test_revoke_user_sessions_except_current_returns_hashes(identity_db: IdentityDb) -> None:
    user, other = await _user(identity_db.database), await _user(identity_db.database)
    rows = [
        (await _new_session(identity_db.database, user.id, expires_in=timedelta(hours=1)))[1]
        for _ in range(3)
    ]
    _, other_row = await _new_session(identity_db.database, other.id, expires_in=timedelta(hours=1))
    current, *rest = rows
    async with identity_db.database.global_session() as session:
        repo = IdentityRepo(session)
        assert await repo.revoke_session(rest[0].id, _NOW)
        hashes = await repo.revoke_user_sessions(user.id, _NOW, except_session_id=current.id)
        assert hashes == [rest[1].token_hash]
        assert await repo.revoke_user_sessions(user.id, _NOW) == [current.token_hash]
        assert await repo.revoke_user_sessions(user.id, _NOW) == []
        revoked = await session.scalar(select(Session.revoked_at).where(Session.id == other_row.id))
    assert revoked is None


async def test_email_token_hash_only_and_single_use(identity_db: IdentityDb) -> None:
    user = await _user(identity_db.database)
    async with identity_db.database.global_session() as session:
        issued = await IdentityRepo(session).issue_email_token(
            user_id=user.id, purpose="verify_email", now=_NOW, ttl=EMAIL_VERIFY_TTL
        )
    assert issued.plaintext not in repr(issued)
    async with identity_db.database.global_session() as session:
        row = (await session.execute(select(EmailToken).where(EmailToken.user_id == user.id))).scalar_one()
        repo = IdentityRepo(session)
        assert await repo.consume_email_token(issued.plaintext, "verify_email", _NOW) == user.id
        assert await repo.consume_email_token(issued.plaintext, "verify_email", _NOW) is None
    assert row.token_hash == sha256_hex(issued.plaintext)
    assert (row.expires_at, row.created_at, row.used_at) == (_NOW + EMAIL_VERIFY_TTL, _NOW, _NOW)


async def test_email_token_expired_or_wrong_purpose_none(identity_db: IdentityDb) -> None:
    user = await _user(identity_db.database)
    async with identity_db.database.global_session() as session:
        repo = IdentityRepo(session)
        issued = await repo.issue_email_token(
            user_id=user.id, purpose="reset_password", now=_NOW, ttl=PASSWORD_RESET_TTL
        )
        assert await repo.consume_email_token(issued.plaintext, "verify_email", _NOW) is None
        assert (
            await repo.consume_email_token(issued.plaintext, "reset_password", _NOW + PASSWORD_RESET_TTL)
            is None
        )
        assert await repo.consume_email_token("unknown", "reset_password", _NOW) is None
        assert await repo.consume_email_token(issued.plaintext, "reset_password", _NOW) == user.id


async def test_count_issued_since(identity_db: IdentityDb) -> None:
    user = await _user(identity_db.database)
    async with identity_db.database.global_session() as session:
        repo = IdentityRepo(session)
        for minutes in (0, 30, 90):
            await repo.issue_email_token(
                user_id=user.id,
                purpose="verify_email",
                now=_NOW + timedelta(minutes=minutes),
                ttl=EMAIL_VERIFY_TTL,
            )
        await repo.issue_email_token(
            user_id=user.id, purpose="reset_password", now=_NOW, ttl=PASSWORD_RESET_TTL
        )
        since = _NOW + timedelta(minutes=30)
        assert await repo.count_issued_since(user.id, "verify_email", since) == 2
        assert await repo.count_issued_since(user.id, "verify_email", _NOW) == 3
        assert await repo.count_issued_since(user.id, "reset_password", since) == 0
