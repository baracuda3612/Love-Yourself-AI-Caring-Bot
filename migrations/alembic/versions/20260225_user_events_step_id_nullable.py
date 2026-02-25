"""Make user_events.step_id nullable for plan-level telemetry events.

Revision ID: 20260225_user_events_step_id_nullable
Revises: 20260210_add_plan_instances_versions
Create Date: 2026-02-25
"""

from alembic import op


revision = "20260225_user_events_step_id_nullable"
down_revision = "20260210_add_plan_instances_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("user_events", "step_id", nullable=True)


def downgrade() -> None:
    # Note: downgrade will fail if NULL values exist — intentional.
    op.alter_column("user_events", "step_id", nullable=False)
