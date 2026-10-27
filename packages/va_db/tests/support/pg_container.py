"""A disposable ``pgvector/pgvector:pg16`` server with the spec roles and an RLS probe table."""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

PG_IMAGE: Final = "pgvector/pgvector:pg16"
# The server is disposable, so durability is traded for speed: with fsync on, the forced checkpoints of
# DROP DATABASE (and heavy DDL) on one container per xdist worker outran asyncpg's 30 s command timeout.
SERVER_COMMAND: Final = "postgres -c fsync=off -c synchronous_commit=off -c full_page_writes=off"

# Roles as in specs/03-data-model.md §2 (no passwords: the test logs in as the superuser and assumes
# each role through the connection's ``role`` startup parameter)
# and a tenant table with the exact §10 policy.
SETUP_SQL: Final = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'va_app') THEN
        CREATE ROLE va_app LOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'va_system') THEN
        CREATE ROLE va_system LOGIN BYPASSRLS;
    END IF;
END
$$;
CREATE TABLE rls_probe (org_id uuid NOT NULL, v text NOT NULL);
ALTER TABLE rls_probe ENABLE ROW LEVEL SECURITY;
ALTER TABLE rls_probe FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON rls_probe
  USING (org_id = nullif(current_setting('app.org_id', true), '')::uuid)
  WITH CHECK (org_id = nullif(current_setting('app.org_id', true), '')::uuid);
GRANT SELECT, INSERT ON rls_probe TO va_app, va_system;
"""


@contextmanager
def running_postgres() -> Iterator[str]:
    """Start the server, apply :data:`SETUP_SQL`, and yield its ``postgresql+asyncpg://`` URL."""
    from testcontainers.community.postgres import (  # noqa: PLC0415 - imported only when Docker exists
        PostgresContainer,
    )

    password = secrets.token_hex(16)
    server = PostgresContainer(PG_IMAGE, username="va", password=password, dbname="va", driver=None)
    with server.with_command(SERVER_COMMAND) as container:
        result = container.exec(["psql", "-U", "va", "-d", "va", "-v", "ON_ERROR_STOP=1", "-c", SETUP_SQL])
        if result.exit_code != 0:
            msg = f"database setup failed: {result.output.decode(errors='replace')}"
            raise RuntimeError(msg)
        host = container.get_container_host_ip()
        port = container.get_exposed_port(5432)
        yield f"postgresql+asyncpg://va:{password}@{host}:{port}/va"
