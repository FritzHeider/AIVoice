"""``call_events.event_type`` (specs/03 §6; specs/19 §9 Decision P06-T06a).

The SSE call event stream names every frame after the event type (``event: call.turn``), which the row
did not record. ``ADD COLUMN`` on the partitioned parent adds the column to every partition and appends it
last, which is why the specs/03 DDL lists it after ``created_at``. Rows written before this revision keep
NULL (their frames carry no ``event:`` line). The table's existing grants cover the new column.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-03
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADD_COLUMN: Final = "ALTER TABLE call_events ADD COLUMN event_type text"
DROP_COLUMN: Final = "ALTER TABLE call_events DROP COLUMN event_type"


def upgrade() -> None:
    op.execute(sa.text(ADD_COLUMN))


def downgrade() -> None:
    op.execute(sa.text(DROP_COLUMN))
