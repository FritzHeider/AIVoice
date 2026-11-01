"""Add ``telnyx_events.process_attempts``: processing attempts of a stored webhook (specs/03 §4).

The telephony edge's ``TelnyxEventReprocessor`` (specs/06-telephony-telnyx.md §4.1) retries a failed event at
most 5 times, so every processing attempt is counted on the row. ``ADD COLUMN`` appends the column, which is
why the specs/03 DDL lists it last. The table's existing grants cover the new column.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-01
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADD_COLUMN: Final = "ALTER TABLE telnyx_events ADD COLUMN process_attempts integer NOT NULL DEFAULT 0"
DROP_COLUMN: Final = "ALTER TABLE telnyx_events DROP COLUMN process_attempts"


def upgrade() -> None:
    op.execute(sa.text(ADD_COLUMN))


def downgrade() -> None:
    op.execute(sa.text(DROP_COLUMN))
