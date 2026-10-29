"""MFA enrolment state on ``users`` (specs/04-auth-tenancy-rbac.md §4.5; specs/03-data-model.md §3).

``users`` is a global identity table (no ``org_id``, no RLS), so :class:`MfaRepo` runs on
``Database.global_session()`` like :class:`va_db.repositories.IdentityRepo`. The TOTP secret arrives already
encrypted (``Envelope.platform_encrypt``, specs/28-security.md §3 (j)) and recovery codes as argon2id hashes;
every write is one conditional ``UPDATE`` that reports whether it applied, so concurrent requests cannot both
enable MFA or both spend the same recovery code.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ColumnElement, Text, any_, func, literal, update
from sqlalchemy.ext.asyncio import AsyncSession

from va_db.models import User

__all__ = ["MfaRepo"]


class MfaRepo:
    """Conditional writes of the ``users`` MFA columns (secret, enabled flag, recovery code hashes)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _update(
        self, user_id: uuid.UUID, conditions: tuple[ColumnElement[bool], ...], **values: Any
    ) -> bool:
        result = await self.session.execute(
            update(User).where(User.id == user_id, *conditions).values(**values).returning(User.id)
        )
        return result.scalar_one_or_none() is not None

    async def begin_setup(self, user_id: uuid.UUID, secret_enc: bytes) -> bool:
        """Store a pending TOTP secret (replacing any earlier one); ``False`` when MFA is already enabled."""
        return await self._update(user_id, (User.mfa_enabled.is_(False),), mfa_totp_secret_enc=secret_enc)

    async def enable(self, user_id: uuid.UUID, secret_enc: bytes, code_hashes: list[str]) -> bool:
        """Enable MFA with ``code_hashes`` as the recovery codes.

        Applies only while MFA is disabled and the pending secret is still ``secret_enc`` (the blob the caller
        verified the code against), so a setup that replaced the secret meanwhile makes this return ``False``.
        """
        return await self._update(
            user_id,
            (User.mfa_enabled.is_(False), User.mfa_totp_secret_enc == secret_enc),
            mfa_enabled=True,
            mfa_recovery_codes_hash=code_hashes,
        )

    async def disable(self, user_id: uuid.UUID) -> bool:
        """Disable MFA and clear the secret and recovery codes; ``False`` when MFA was not enabled."""
        return await self._update(
            user_id,
            (User.mfa_enabled.is_(True),),
            mfa_enabled=False,
            mfa_totp_secret_enc=None,
            mfa_recovery_codes_hash=[],
        )

    async def consume_recovery_code(self, user_id: uuid.UUID, code_hash: str) -> bool:
        """Remove ``code_hash`` from the recovery codes; ``False`` when it is absent or MFA is disabled.

        One conditional ``UPDATE``: of two concurrent requests spending the same code, only one sees it.
        """
        return await self._update(
            user_id,
            (User.mfa_enabled.is_(True), literal(code_hash, Text) == any_(User.mfa_recovery_codes_hash)),
            mfa_recovery_codes_hash=func.array_remove(User.mfa_recovery_codes_hash, code_hash),
        )
