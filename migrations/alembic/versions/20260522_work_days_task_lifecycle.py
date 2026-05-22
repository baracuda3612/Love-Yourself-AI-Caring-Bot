"""work_days active_days + step_status + expires_at

Revision ID: 20260522_work_days
Revises: 20260210_add_plan_instances_versions
Create Date: 2026-05-22
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "20260522_work_days"
down_revision = "20260210_add_plan_instances_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- user_profiles: active_days ---
    op.add_column(
        "user_profiles",
        sa.Column(
            "active_days",
            JSONB,
            nullable=True,
            server_default='["MON","TUE","WED","THU","FRI"]',
        ),
    )

    # --- ai_plan_steps: step_status + expires_at ---
    op.add_column(
        "ai_plan_steps",
        sa.Column(
            "step_status",
            sa.String(20),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "ai_plan_steps",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Backfill step_status from existing boolean columns.
    op.execute("""
        UPDATE ai_plan_steps SET step_status =
            CASE
                WHEN is_completed = TRUE              THEN 'completed'
                WHEN skipped      = TRUE              THEN 'skipped'
                WHEN canceled_by_adaptation = TRUE    THEN 'expired'
                ELSE 'pending'
            END
    """)


def downgrade() -> None:
    op.drop_column("ai_plan_steps", "expires_at")
    op.drop_column("ai_plan_steps", "step_status")
    op.drop_column("user_profiles", "active_days")
