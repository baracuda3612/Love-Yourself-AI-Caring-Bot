"""Persist setup callback revisions and unapplied weekday selections."""

from alembic import op
import sqlalchemy as sa

revision = "20261104_onboarding_controls"
down_revision = "20261022_privacy_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("onboarding_progress", sa.Column(
        "revision", sa.BigInteger(), nullable=False, server_default="0"
    ))
    op.add_column("onboarding_progress", sa.Column(
        "last_text_message_id", sa.BigInteger(), nullable=False, server_default="0"
    ))
    op.add_column("onboarding_progress", sa.Column("draft_days", sa.Integer()))
    op.create_check_constraint(
        "ck_onboarding_progress_controls", "onboarding_progress",
        "revision >= 0 AND (draft_days IS NULL OR draft_days BETWEEN 0 AND 127)",
    )


def downgrade():
    op.drop_constraint("ck_onboarding_progress_controls", "onboarding_progress", type_="check")
    op.drop_column("onboarding_progress", "draft_days")
    op.drop_column("onboarding_progress", "last_text_message_id")
    op.drop_column("onboarding_progress", "revision")
