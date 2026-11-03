"""Webhook signing secret rotation columns on ``orgs`` (specs/03 §3; specs/19 §10 Decision P06-T04a).

``webhook_signing_secret_created_at`` records when the current secret was issued (backfilled with the org's
``created_at``); ``webhook_signing_secret_prev_enc`` holds the envelope-encrypted previous secret, which the
dispatcher keeps signing with until ``webhook_signing_secret_prev_expires_at``. ``ADD COLUMN`` appends the
columns, which is why the specs/03 DDL lists them last. The table's existing grants cover the new columns.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-03
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADD_COLUMNS: Final = (
    "ALTER TABLE orgs ADD COLUMN webhook_signing_secret_created_at timestamptz NOT NULL DEFAULT now()",
    "ALTER TABLE orgs ADD COLUMN webhook_signing_secret_prev_enc bytea",
    "ALTER TABLE orgs ADD COLUMN webhook_signing_secret_prev_expires_at timestamptz",
)
BACKFILL: Final = "UPDATE orgs SET webhook_signing_secret_created_at = created_at"
DROP_COLUMNS: Final = (
    "ALTER TABLE orgs DROP COLUMN webhook_signing_secret_prev_expires_at",
    "ALTER TABLE orgs DROP COLUMN webhook_signing_secret_prev_enc",
    "ALTER TABLE orgs DROP COLUMN webhook_signing_secret_created_at",
)


def upgrade() -> None:
    for statement in (*ADD_COLUMNS, BACKFILL):
        op.execute(sa.text(statement))


def downgrade() -> None:
    for statement in DROP_COLUMNS:
        op.execute(sa.text(statement))
