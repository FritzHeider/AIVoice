"""Add ``sessions.mfa_passed_at``: when the session passed MFA (specs/03-data-model.md §3).

"Recent auth" (specs/04-auth-tenancy-rbac.md §4.1 ``password/change``) counts from login or MFA verification,
so the session records the MFA time. ``ADD COLUMN`` appends the column, which is why the specs/03 DDL lists it
last. The table's existing grants cover the new column.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-29
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADD_COLUMN: Final = "ALTER TABLE sessions ADD COLUMN mfa_passed_at timestamptz"
DROP_COLUMN: Final = "ALTER TABLE sessions DROP COLUMN mfa_passed_at"


def upgrade() -> None:
    op.execute(sa.text(ADD_COLUMN))


def downgrade() -> None:
    op.execute(sa.text(DROP_COLUMN))
