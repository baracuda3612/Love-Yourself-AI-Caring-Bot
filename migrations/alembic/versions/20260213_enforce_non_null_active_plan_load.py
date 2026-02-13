"""Enforce non-null load for active plans and harden ai_plans.load column.

Revision ID: 20260213_enforce_non_null_active_plan_load
Revises: 20260210_add_plan_instances_versions
Create Date: 2026-02-13
"""

from alembic import op
import sqlalchemy as sa


revision = "20260213_enforce_non_null_active_plan_load"
down_revision = "20260210_add_plan_instances_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    active_without_load = bind.execute(
        sa.text(
            """
            SELECT id, user_id
            FROM ai_plans
            WHERE status = 'active' AND load IS NULL
            LIMIT 1
            """
        )
    ).first()
    if active_without_load:
        raise RuntimeError(
            "Migration aborted: found active ai_plans row with NULL load "
            f"(plan_id={active_without_load.id}, user_id={active_without_load.user_id})."
        )

    any_without_load = bind.execute(
        sa.text("SELECT id FROM ai_plans WHERE load IS NULL LIMIT 1")
    ).first()
    if any_without_load:
        raise RuntimeError(
            "Migration aborted: ai_plans.load contains NULL values. "
            "Backfill explicitly before enforcing NOT NULL."
        )

    op.alter_column("ai_plans", "load", existing_type=sa.String(length=20), nullable=False)


def downgrade() -> None:
    op.alter_column("ai_plans", "load", existing_type=sa.String(length=20), nullable=True)
