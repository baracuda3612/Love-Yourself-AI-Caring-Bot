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
    # SQL-first migration (PostgreSQL): drop NOT NULL only when needed.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'user_events'
                  AND column_name = 'step_id'
                  AND is_nullable = 'NO'
            ) THEN
                ALTER TABLE user_events ALTER COLUMN step_id DROP NOT NULL;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    # Note: downgrade will fail if NULL values exist — intentional.
    op.execute("ALTER TABLE user_events ALTER COLUMN step_id SET NOT NULL;")
