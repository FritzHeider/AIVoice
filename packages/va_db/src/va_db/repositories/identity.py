"""Users, dashboard sessions, and email-link tokens (specs/04-auth-tenancy-rbac.md §4.1, §4.3).

These tables (specs/03-data-model.md §3) have no ``org_id`` and no RLS, so :class:`IdentityRepo` works on
``Database.global_session()`` or on a ``tenant_session`` that also writes tenant rows in the same transaction
(specs/03-data-model.md §2). Session and email tokens are opaque (``va_common.auth.issue_token``); only their
``sha256_hex`` is stored, and the plaintext is returned once to the caller.
"""

from __future__ import annotations

import ipaddress
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from va_common.auth import IssuedToken, issue_token, sha256_hex
from va_db.models import EmailToken, EmailTokenPurpose, Session, User
from va_db.repositories.base import conflict_savepoint

__all__ = ["EMAIL_VERIFY_TTL", "PASSWORD_RESET_TTL", "IdentityRepo", "IssuedSession", "NewUser"]

EMAIL_VERIFY_TTL: Final = timedelta(hours=24)
PASSWORD_RESET_TTL: Final = timedelta(hours=1)
DUPLICATE_EMAIL_MESSAGE: Final = "An account with this email already exists."


@dataclass(frozen=True, slots=True)
class NewUser:
    """The columns of a user to create; ``password_hash`` is ``None`` for SSO-only users."""

    email: str
    name: str = ""
    password_hash: str | None = field(default=None, repr=False)
    email_verified_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """A new session row and its cookie token (shown once; ``repr`` never includes it)."""

    token: str = field(repr=False)
    session: Session


def _checked_ip(ip: str | None) -> str | None:
    """``ip`` unchanged after validating it as an IPv4/IPv6 address (``ValueError`` otherwise)."""
    if ip is not None:
        ipaddress.ip_address(ip)
    return ip


def _active_session(token: str, now: datetime) -> tuple[ColumnElement[bool], ...]:
    """Conditions matching the unrevoked, unexpired session whose cookie token is ``token``."""
    return (Session.token_hash == sha256_hex(token), Session.revoked_at.is_(None), Session.expires_at > now)


def _usable_email_token(
    token: str, purpose: EmailTokenPurpose, now: datetime
) -> tuple[ColumnElement[bool], ...]:
    """Conditions matching the unused, unexpired ``purpose`` email token whose plaintext is ``token``."""
    return (
        EmailToken.token_hash == sha256_hex(token),
        EmailToken.purpose == purpose,
        EmailToken.used_at.is_(None),
        EmailToken.expires_at > now,
    )


class IdentityRepo:
    """Data access for ``users``, ``sessions``, and ``email_tokens``."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_user(self, new: NewUser) -> User:
        """Insert a user; ``CONFLICT`` (``field: "email"``) when the email exists in any letter case."""
        user = User(
            email=new.email.strip(),
            name=new.name,
            password_hash=new.password_hash,
            email_verified_at=new.email_verified_at,
        )
        async with conflict_savepoint(self.session, DUPLICATE_EMAIL_MESSAGE, field="email"):
            self.session.add(user)
        await self.session.refresh(user)
        return user

    async def get_user(self, user_id: uuid.UUID) -> User | None:
        return await self.session.get(User, user_id)

    async def get_user_by_email(self, email: str) -> User | None:
        """The user with ``email`` (surrounding whitespace ignored, case-insensitive ``citext``)."""
        return await self.session.scalar(select(User).where(User.email == email.strip()))

    async def mark_email_verified(self, user_id: uuid.UUID, now: datetime) -> None:
        """Set ``email_verified_at`` to ``now`` unless it is already set (the first verification wins)."""
        await self.session.execute(
            update(User)
            .where(User.id == user_id, User.email_verified_at.is_(None))
            .values(email_verified_at=now)
        )

    async def set_password_hash(self, user_id: uuid.UUID, password_hash: str) -> None:
        await self.session.execute(update(User).where(User.id == user_id).values(password_hash=password_hash))

    async def record_login(self, user_id: uuid.UUID, now: datetime) -> None:
        await self.session.execute(update(User).where(User.id == user_id).values(last_login_at=now))

    async def create_session(  # noqa: PLR0913 - keyword-only session attributes
        self,
        *,
        user_id: uuid.UUID,
        active_org_id: uuid.UUID | None,
        expires_at: datetime,
        ip: str | None,
        user_agent: str | None,
        mfa_passed: bool = False,
        mfa_passed_at: datetime | None = None,
        created_at: datetime | None = None,
    ) -> IssuedSession:
        """Insert a session for ``user_id``; ``ValueError`` when ``ip`` is not an IP address.

        ``created_at`` defaults to the database clock; callers enforcing the absolute session lifetime pass
        their own clock's time so both bounds come from one clock.
        """
        issued = issue_token()
        row = Session(
            user_id=user_id,
            token_hash=issued.token_hash,
            active_org_id=active_org_id,
            ip=_checked_ip(ip),
            user_agent=user_agent,
            mfa_passed=mfa_passed,
            mfa_passed_at=mfa_passed_at,
            expires_at=expires_at,
        )
        if created_at is not None:
            row.created_at = created_at
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)
        return IssuedSession(token=issued.plaintext, session=row)

    async def get_active_session(self, token: str, now: datetime) -> Session | None:
        """The unrevoked, unexpired session whose cookie token is ``token``."""
        return await self.session.scalar(select(Session).where(*_active_session(token, now)))

    async def get_active_session_with_user(self, token: str, now: datetime) -> tuple[Session, User] | None:
        """:meth:`get_active_session` joined with its user in one query (any user status)."""
        statement = (
            select(Session, User).join(User, User.id == Session.user_id).where(*_active_session(token, now))
        )
        row = (await self.session.execute(statement)).first()
        if row is None:
            return None
        session_row, user = row
        return session_row, user

    async def extend_session(self, session_id: uuid.UUID, expires_at: datetime) -> None:
        await self.session.execute(
            update(Session).where(Session.id == session_id).values(expires_at=expires_at)
        )

    async def set_active_org(self, session_id: uuid.UUID, org_id: uuid.UUID) -> None:
        await self.session.execute(
            update(Session).where(Session.id == session_id).values(active_org_id=org_id)
        )

    async def set_mfa_passed(self, session_id: uuid.UUID, now: datetime) -> None:
        """Mark the session as having passed MFA at ``now`` (recent auth counts from it)."""
        await self.session.execute(
            update(Session).where(Session.id == session_id).values(mfa_passed=True, mfa_passed_at=now)
        )

    async def revoke_session(self, session_id: uuid.UUID, now: datetime) -> bool:
        """Revoke an unrevoked session; whether a row changed."""
        result = await self.session.execute(
            update(Session)
            .where(Session.id == session_id, Session.revoked_at.is_(None))
            .values(revoked_at=now)
            .returning(Session.id)
        )
        return result.scalar_one_or_none() is not None

    async def revoke_user_sessions(
        self, user_id: uuid.UUID, now: datetime, *, except_session_id: uuid.UUID | None = None
    ) -> list[str]:
        """Revoke every unrevoked session of the user (but ``except_session_id``); their token hashes."""
        statement = update(Session).where(Session.user_id == user_id, Session.revoked_at.is_(None))
        if except_session_id is not None:
            statement = statement.where(Session.id != except_session_id)
        result = await self.session.execute(statement.values(revoked_at=now).returning(Session.token_hash))
        return sorted(result.scalars().all())

    async def issue_email_token(
        self, *, user_id: uuid.UUID, purpose: EmailTokenPurpose, now: datetime, ttl: timedelta
    ) -> IssuedToken:
        """Store a single-use token for ``purpose`` created at ``now``, valid until ``now + ttl``.

        ``created_at`` comes from the caller's clock so :meth:`count_issued_since` agrees with it; the
        plaintext goes out by email only.
        """
        issued = issue_token()
        self.session.add(
            EmailToken(
                user_id=user_id,
                purpose=purpose,
                token_hash=issued.token_hash,
                expires_at=now + ttl,
                created_at=now,
            )
        )
        await self.session.flush()
        return issued

    async def peek_email_token(
        self, token: str, purpose: EmailTokenPurpose, now: datetime
    ) -> uuid.UUID | None:
        """The user of a token :meth:`consume_email_token` would accept now, without using it up.

        Lets a caller validate the rest of a request first, so a rejected request does not burn the link.
        """
        user_id: uuid.UUID | None = await self.session.scalar(
            select(EmailToken.user_id).where(*_usable_email_token(token, purpose, now))
        )
        return user_id

    async def consume_email_token(
        self, token: str, purpose: EmailTokenPurpose, now: datetime
    ) -> uuid.UUID | None:
        """Mark the token used and return its user; ``None`` when unknown, used, expired, or another purpose.

        One conditional ``UPDATE``, so concurrent consumers cannot both succeed.
        """
        result = await self.session.execute(
            update(EmailToken)
            .where(*_usable_email_token(token, purpose, now))
            .values(used_at=now)
            .returning(EmailToken.user_id)
        )
        user_id: uuid.UUID | None = result.scalar_one_or_none()
        return user_id

    async def retire_email_tokens(self, user_id: uuid.UUID, purpose: EmailTokenPurpose, now: datetime) -> int:
        """Mark every unused ``purpose`` token of the user used at ``now``; how many were retired."""
        result = await self.session.execute(
            update(EmailToken)
            .where(EmailToken.user_id == user_id, EmailToken.purpose == purpose, EmailToken.used_at.is_(None))
            .values(used_at=now)
            .returning(EmailToken.id)
        )
        return len(result.scalars().all())

    async def count_issued_since(
        self, user_id: uuid.UUID, purpose: EmailTokenPurpose, since: datetime
    ) -> int:
        """How many ``purpose`` tokens were issued to the user at or after ``since``."""
        statement = select(func.count()).where(
            EmailToken.user_id == user_id, EmailToken.purpose == purpose, EmailToken.created_at >= since
        )
        count: int = (await self.session.execute(statement)).scalar_one()
        return count
