"""Builders for rows that model integration tests need as parents (orgs, users)."""

from __future__ import annotations

import os
import secrets
from collections.abc import Awaitable, Callable
from typing import Any, get_args

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from va_db.base import Base
from va_db.models import Org, User

__all__ = ["ModelBuilder", "assert_enum_check_enforced", "flushed", "make_org", "make_user"]

type ModelBuilder = Callable[[AsyncSession], Awaitable[Base]]


async def flushed[M: Base](session: AsyncSession, model: M) -> M:
    """Insert ``model`` and reload it, so server defaults are visible."""
    session.add(model)
    await session.flush()
    await session.refresh(model)
    return model


async def make_org(session: AsyncSession, **values: Any) -> Org:
    org = Org(
        name="Acme Dental",
        slug=f"acme-{secrets.token_hex(4)}",
        webhook_signing_secret_enc=os.urandom(32),
        **values,
    )
    return await flushed(session, org)


async def make_user(session: AsyncSession, email: str | None = None) -> User:
    return await flushed(session, User(email=email or f"{secrets.token_hex(4)}@example.test"))


async def assert_enum_check_enforced(
    session: AsyncSession, builder: ModelBuilder, column: str, literal: Any
) -> None:
    """Every ``literal`` value inserts; ``"bogus"`` violates ``<table>_<column>_check``."""
    for value in get_args(literal):
        model = await builder(session)
        setattr(model, column, value)
        async with session.begin_nested():
            await flushed(session, model)
        assert getattr(model, column) == value
    model = await builder(session)
    setattr(model, column, "bogus")
    with pytest.raises(IntegrityError, match=f"{model.__tablename__}_{column}_check"):
        async with session.begin_nested():
            await flushed(session, model)
