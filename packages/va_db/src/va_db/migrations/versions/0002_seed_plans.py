"""Seed the plan catalogue: specs/24-billing-and-limits.md §2.

Values are the §2 micro-dollar seed values. "custom" Enterprise prices use the "enterprise default"
values; per-org overrides live in ``orgs.limits``. "unlimited" caps are NULL. ``tts_rate_micros_per_1k_chars``
is the §2 TTS standalone price ($0.00012/char) for 1 000 characters. ``stripe_price_id`` stays NULL:
deployments set it per environment. The specs/24 §2 Decision (2026-10-27) records these choices.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-27
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


PLAN_IDS: Final = ("start", "build", "scale", "enterprise")

SEED_PLANS: Final = """
INSERT INTO plans (
  id, name, monthly_fee_micros, connected_rate_micros_per_min, transfer_rate_micros_per_min,
  min_attempt_micros, sms_rate_micros, widget_message_micros, tts_rate_micros_per_1k_chars,
  daily_call_cap, hourly_call_cap, concurrency_cap, voice_clone_cap, features, is_public
) VALUES
  ('start', 'Start', 0, 120000, 50000, 15000, 20000, 10000, 120000, 100, 100, 10, 1, '{}'::text[], true),
  ('build', 'Build', 249000000, 100000, 40000, 15000, 20000, 10000, 120000, 2000, 1000, 50, 5,
   ARRAY['local_presence', 'analysis_schema', 'webhooks_org']::text[], true),
  ('scale', 'Scale', 449000000, 90000, 30000, 15000, 20000, 10000, 120000, 5000, 1000, 100, 15,
   ARRAY['local_presence', 'analysis_schema', 'webhooks_org', 'citations',
         'white_label_widget']::text[], true),
  ('enterprise', 'Enterprise', 0, 80000, 25000, 15000, 20000, 10000, 120000, NULL, NULL, NULL, NULL,
   ARRAY['local_presence', 'analysis_schema', 'webhooks_org', 'citations', 'white_label_widget',
         'custom_guard_rails', 'number_pools', 'experiments', 'sso', 'hipaa', 'dedicated_infra',
         'custom_llm']::text[], true)
"""

DELETE_PLANS: Final = "DELETE FROM plans WHERE id IN ('start', 'build', 'scale', 'enterprise')"


def upgrade() -> None:
    op.execute(sa.text(SEED_PLANS))


def downgrade() -> None:
    """Delete exactly the seeded plans; an org_subscriptions reference fails it instead of losing data."""
    op.execute(sa.text(DELETE_PLANS))
