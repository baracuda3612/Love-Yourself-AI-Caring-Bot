"""Separate successor scheduling from completion-report delivery."""

import sqlalchemy as sa
from alembic import op

revision = "20261111_plan_continuation"
down_revision = "20261104_menu_input"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("plan_completions", sa.Column("schedule_wake_at", sa.DateTime(timezone=True)))
    op.add_column("plan_completions", sa.Column(
        "legacy_schedule_repair", sa.Boolean(), nullable=False, server_default=sa.false(),
    ))
    op.execute("""
        UPDATE plan_completions AS completion
        SET schedule_pending = TRUE, schedule_wake_at = now(), legacy_schedule_repair = TRUE
        FROM ai_plans AS successor
        WHERE successor.id = completion.successor_plan_id
          AND successor.status = 'active'
          AND completion.state NOT IN ('sent', 'released')
    """)
    op.execute("""
        UPDATE plan_completions SET schedule_wake_at = now()
        WHERE schedule_pending AND schedule_wake_at IS NULL
    """)


def downgrade():
    op.drop_column("plan_completions", "legacy_schedule_repair")
    op.drop_column("plan_completions", "schedule_wake_at")
