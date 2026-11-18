"""Persist the latest automatic continuation attempt per completed cycle."""

import sqlalchemy as sa
from alembic import op

revision = "20261111_continuation_attempt"
down_revision = "20261111_plan_continuation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "plan_continuation_attempts",
        sa.Column("source_plan_id", sa.Integer(), sa.ForeignKey("ai_plans.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retry_at", sa.DateTime(timezone=True)),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("failure_code", sa.String(32)),
        sa.CheckConstraint("attempt > 0", name="ck_continuation_attempt_positive"),
        sa.CheckConstraint(
            "(state IN ('started', 'retry_wait') AND retry_at IS NOT NULL) OR "
            "(state IN ('created', 'superseded') AND retry_at IS NULL)",
            name="ck_continuation_attempt_state",
        ),
    )
    op.create_index("ix_continuation_attempt_retry", "plan_continuation_attempts", ["retry_at"])


def downgrade():
    op.drop_table("plan_continuation_attempts")
