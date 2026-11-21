"""Persist overdue schedule reconciliation across admission and restarts."""

import sqlalchemy as sa
from alembic import op

revision = "20261119_schedule_reconcile"
down_revision = "20261111_continuation_attempt"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ai_plans", sa.Column("schedule_reconcile_at", sa.DateTime(timezone=True)))
    op.add_column("ai_plans", sa.Column("schedule_switch_source", sa.String(160)))
    op.create_index(
        "ix_ai_plans_schedule_reconcile",
        "ai_plans", ["schedule_reconcile_at", "id"],
        postgresql_where=sa.text("schedule_reconcile_at IS NOT NULL"),
    )


def downgrade():
    op.drop_index("ix_ai_plans_schedule_reconcile", table_name="ai_plans")
    op.drop_column("ai_plans", "schedule_switch_source")
    op.drop_column("ai_plans", "schedule_reconcile_at")
