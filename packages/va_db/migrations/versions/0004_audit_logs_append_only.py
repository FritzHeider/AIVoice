"""Make ``audit_logs`` append-only for ``va_app`` (specs/28-security.md §6.3, threat T15; specs/04 §13).

``0001_initial`` grants ``va_app`` every DML privilege on every table. The application role may only INSERT
and SELECT audit rows, so this revision revokes UPDATE and DELETE. ``va_system`` keeps them for the retention
purge (``retention_days``). Foreign-key cascades from ``orgs`` run as the table owner and are unaffected.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-29
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REVOKE: Final = "REVOKE UPDATE, DELETE ON TABLE audit_logs FROM va_app"
REGRANT: Final = "GRANT UPDATE, DELETE ON TABLE audit_logs TO va_app"


def upgrade() -> None:
    op.execute(sa.text(REVOKE))


def downgrade() -> None:
    op.execute(sa.text(REGRANT))
