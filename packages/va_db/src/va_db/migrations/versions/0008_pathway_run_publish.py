"""``pathway_test_runs.publish`` (specs/03 §5; specs/12 §4 Decision P07-T11e2).

A regression run started by a publish records that publish (environment, version, graph hash, and its outcome:
``pending`` until a blocking run settles it, then ``published``, ``blocked``, ``stale``, or
``version_unavailable``). ``ADD COLUMN`` appends the column after ``updated_at``, which is why the specs/03
DDL lists it last. Runs of other kinds keep NULL. The table's existing grants cover the new column.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-04
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADD_COLUMN: Final = "ALTER TABLE pathway_test_runs ADD COLUMN publish jsonb"
DROP_COLUMN: Final = "ALTER TABLE pathway_test_runs DROP COLUMN publish"


def upgrade() -> None:
    op.execute(sa.text(ADD_COLUMN))


def downgrade() -> None:
    op.execute(sa.text(DROP_COLUMN))
