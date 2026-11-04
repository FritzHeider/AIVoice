"""``call_turns.pathway_turn`` (specs/03 §6; specs/11 §12 Decision P07-T12b).

The engine turn index of a pathway call's transcript row: ``turn_index`` counts every pipeline turn (opener,
caller, agent), while the engine's events carry its own ``turn_index`` (+1 per accepted caller turn), so the
Call Object's ``pathway_logs`` groups a pathway call's rows by ``pathway_turn``. ``ADD COLUMN`` appends the
column after ``updated_at``, which is why the specs/03 DDL lists it last. Rows of other runners keep NULL.
The table's existing grants cover the new column.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-04
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADD_COLUMN: Final = "ALTER TABLE call_turns ADD COLUMN pathway_turn integer"
DROP_COLUMN: Final = "ALTER TABLE call_turns DROP COLUMN pathway_turn"


def upgrade() -> None:
    op.execute(sa.text(ADD_COLUMN))


def downgrade() -> None:
    op.execute(sa.text(DROP_COLUMN))
