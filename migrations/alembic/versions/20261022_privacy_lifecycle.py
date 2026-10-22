"""Privacy lifecycle; stop old binaries. Never run on durable data before G1."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261022_privacy_lifecycle"
down_revision = "20261015_access_authority"
branch_labels = None
depends_on = None


def upgrade():
    # This is a transactional proof against the actual target, not a claim that
    # production was inspected here. Nonempty speculative stores require review.
    op.execute("""
        LOCK TABLE user_facts, user_daily_logs IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM user_facts) OR EXISTS (SELECT 1 FROM user_daily_logs) THEN
            RAISE EXCEPTION 'sensitive stores nonempty: reviewed privacy disposition required';
          END IF;
        END $$;
    """)
    op.drop_table("user_facts")
    op.drop_table("user_daily_logs")
    op.execute("DROP TYPE IF EXISTS fact_category; DROP TYPE IF EXISTS factcategory")
    op.add_column("chat_history", sa.Column("expires_at", sa.DateTime(timezone=True)))
    op.execute("UPDATE chat_history SET created_at = COALESCE(created_at, now() - interval '90 days'), expires_at = COALESCE(created_at, now() - interval '90 days') + interval '90 days'")
    op.alter_column("chat_history", "created_at", nullable=False)
    op.alter_column("chat_history", "expires_at", nullable=False, server_default=sa.text("now() + interval '90 days'"))
    op.create_check_constraint("ck_chat_retention", "chat_history", "expires_at <= created_at + interval '90 days'")
    op.create_index("ix_chat_history_user_recent", "chat_history", ["user_id", sa.text("created_at DESC"), sa.text("id DESC")])
    op.create_index("ix_chat_history_retention", "chat_history", ["expires_at", "id"])
    op.create_index("ix_feedback_retention", "feedback_events", ["created_at", "id"])
    op.create_index("ix_feedback_coach_source", "feedback_events", ["coach_message_id"])
    op.create_index("ix_feedback_product_source", "feedback_events", ["source_message_id"])
    op.add_column("privacy_notice_versions", sa.Column("notice_text", sa.Text()))
    op.execute("""
        CREATE FUNCTION ly_preserve_notice_text() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF NEW.notice_text IS DISTINCT FROM OLD.notice_text THEN
          RAISE EXCEPTION 'notice text is immutable: publish a new version';
        END IF; RETURN NEW; END $$;
        CREATE TRIGGER tr_notice_text BEFORE UPDATE OF notice_text ON privacy_notice_versions
        FOR EACH ROW EXECUTE FUNCTION ly_preserve_notice_text();
    """)
    op.add_column("report_access_grants", sa.Column("deployment_id", sa.BigInteger(), sa.ForeignKey("deployments.id", ondelete="RESTRICT")))
    # Old unscoped grants are inert; no inferred deployment attribution.
    op.execute("UPDATE report_access_grants SET revoked_at = GREATEST(now(), issued_at), revocation_reason = 'privacy_cutover' WHERE revoked_at IS NULL")
    op.create_index("ix_report_grants_expiry", "report_access_grants", ["expires_at", "id"], postgresql_where=sa.text("revoked_at IS NULL"))
    op.create_table("privacy_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("source_operation_id", sa.String(160), unique=True),
        sa.Column("owner", sa.String(160), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("evidence", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('access','export','correction','deletion')", name="ck_privacy_request_kind"),
        sa.CheckConstraint("state IN ('received','deleting','completed')", name="ck_privacy_request_state"),
        sa.CheckConstraint("due_at > received_at AND (verified_at IS NULL OR verified_at >= received_at)", name="ck_privacy_request_times"),
    )
    op.create_index("ix_privacy_requests_pending", "privacy_requests", ["state", "due_at"])
    op.create_index("ix_privacy_receipts_expiry", "privacy_requests", ["expires_at"], postgresql_where=sa.text("state = 'completed'"))
    # Deleting accounts is immediately invisible to all current access/sends.
    # The durable request is the recovery obligation, not another user state.
    op.execute("""
        CREATE FUNCTION ly_fence_deleting_user() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.is_active AND EXISTS (SELECT 1 FROM privacy_requests
            WHERE user_id=NEW.id AND state='deleting') THEN
            RAISE EXCEPTION 'account deletion pending';
          END IF; RETURN NEW;
        END $$;
        CREATE TRIGGER tr_users_deletion_fence BEFORE UPDATE ON users
          FOR EACH ROW EXECUTE FUNCTION ly_fence_deleting_user();
    """)


def downgrade():
    raise RuntimeError("Forward-only privacy cutover; never restore erased personal data. Rebuild disposable DB or reviewed forward fix.")
