"""Async engines that assume a database role on every new connection (specs/03-data-model.md §2).

Deployments provide a single ``DATABASE_URL`` (specs/02 §6). Each engine logs in with it and sends the
target role as the ``role`` startup parameter of every new connection, so tenant code runs as ``va_app``
(row-level security enforced) and the system repositories run as ``va_system`` (``BYPASSRLS``). A startup
parameter is the session default: no transaction rollback, pool reset, or ``RESET ROLE`` can revert it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from va_common.settings import DatabaseSettings

__all__ = ["COMMAND_TIMEOUT_S", "CONNECT_TIMEOUT_S", "DbRole", "create_engine"]

CONNECT_TIMEOUT_S: Final = 10.0
COMMAND_TIMEOUT_S: Final = 30.0


class DbRole(StrEnum):
    """Database roles an engine can assume (specs/03-data-model.md §2)."""

    APP = "va_app"
    SYSTEM = "va_system"


def create_engine(settings: DatabaseSettings, role: DbRole | None) -> AsyncEngine:
    """An asyncpg engine sized from ``settings``; ``role=None`` keeps the login role (migrations only)."""
    connect_args: dict[str, Any] = {"timeout": CONNECT_TIMEOUT_S, "command_timeout": COMMAND_TIMEOUT_S}
    if role is not None:
        connect_args["server_settings"] = {"role": role.value}
    return create_async_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        pool_pre_ping=True,
        connect_args=connect_args,
    )
