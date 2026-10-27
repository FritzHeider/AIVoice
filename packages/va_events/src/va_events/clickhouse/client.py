"""``CLICKHOUSE_URL`` parsing and the async ClickHouse HTTP client (``clickhouse-connect``).

The client is opened on the ``default`` database and every statement names ``va.`` explicitly
(specs/03-data-model.md §12 Decision), so the applier can create ``va`` itself. Creating the client is a
network round-trip (``clickhouse-connect`` reads the server version and settings), so callers open it inside
a supervised task, never while building a service container.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import SplitResult, unquote, urlsplit

from clickhouse_connect.driver import create_async_client
from clickhouse_connect.driver.asyncclient import AsyncClient

__all__ = ["CLIENT_DATABASE", "ClickHouseTarget", "open_clickhouse", "parse_clickhouse_url"]

CLIENT_DATABASE: Final = "default"
_DEFAULT_PORTS: Final = {"http": 8123, "https": 8443}


@dataclass(frozen=True, slots=True)
class ClickHouseTarget:
    """Connection parameters of an ``http(s)://user:password@host:port/database`` URL."""

    host: str
    port: int
    username: str
    password: str = field(repr=False)
    secure: bool
    database: str


def _http_host(parts: SplitResult) -> str:
    if parts.scheme not in _DEFAULT_PORTS or not parts.hostname:
        msg = "CLICKHOUSE_URL must be an http(s) URL with a host"
        raise ValueError(msg)
    return parts.hostname


def parse_clickhouse_url(url: str) -> ClickHouseTarget:
    """Split ``CLICKHOUSE_URL``; errors never echo the URL because it may carry a password."""
    parts = urlsplit(url)
    host = _http_host(parts)
    return ClickHouseTarget(
        host=host,
        port=parts.port or _DEFAULT_PORTS[parts.scheme],
        username=unquote(parts.username or "default"),
        password=unquote(parts.password or ""),
        secure=parts.scheme == "https",
        database=parts.path.strip("/") or "default",
    )


@asynccontextmanager
async def open_clickhouse(
    url: str, *, connect_timeout_s: float = 5.0, send_receive_timeout_s: float = 30.0
) -> AsyncIterator[AsyncClient]:
    """An initialised ``AsyncClient`` for ``url`` on the ``default`` database, closed on exit."""
    target = parse_clickhouse_url(url)
    client = await create_async_client(
        host=target.host,
        port=target.port,
        username=target.username,
        password=target.password,
        secure=target.secure,
        database=CLIENT_DATABASE,
        connect_timeout=connect_timeout_s,
        send_receive_timeout=send_receive_timeout_s,
    )
    try:
        yield client
    finally:
        await client.close()
