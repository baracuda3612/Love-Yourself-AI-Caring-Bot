"""WP-03.1 clean content cutover; clean zero-user cutover by founder decision."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261003_content_library"
down_revision = "20260905_event_privacy"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    # Founder confirms zero users at this pre-MVP cutover. No legacy archive,
    # plan backfill, or compatibility path is needed. Refuse an unexpected
    # populated installation instead of deleting current user/plan state.
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM users) THEN
            RAISE EXCEPTION 'WP-03.1 clean cutover requires zero users';
          END IF;
        END $$;
    """)
    # Retired compatibility columns/tables are removed wholesale by WP-08.1.
    # Detach their old content FKs now; no archive or compatibility catalogue.
    bind = op.get_bind()
    for table in ("ai_plan_steps", "user_events", "task_stats", "failure_signals"):
        for fk in sa.inspect(bind).get_foreign_keys(table):
            if fk["referred_table"] == "content_library":
                op.drop_constraint(fk["name"], table, type_="foreignkey")
    op.drop_table("content_library")
    op.create_table(
        "content_library",
        sa.Column("exercise_id", sa.Text(), primary_key=True),
        sa.Column("content_version", sa.Integer(), primary_key=True),
        sa.Column("display", postgresql.JSONB(), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=False),
        sa.Column("mechanic", sa.Text(), nullable=False),
        sa.Column("modality", sa.Text(), nullable=False),
        sa.Column("requirements", postgresql.JSONB(), nullable=False),
        sa.Column("cooldown_days", sa.Integer(), nullable=False),
        sa.Column("review_required", sa.Boolean(), nullable=False),
        sa.Column("review_status", sa.Text(), nullable=False),
        sa.Column("review_evidence", postgresql.JSONB()),
        sa.Column("media", postgresql.JSONB()),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("content_version > 0", name="ck_content_version_positive"),
        sa.CheckConstraint("review_status IN ('unreviewed','approved','rejected')", name="ck_content_review_status"),
        sa.CheckConstraint("duration_seconds > 0 AND cooldown_days >= 0 AND mechanic IN ('switch','unload')", name="ck_content_protocol"),
    )
    op.add_column("ai_plan_steps", sa.Column("content_version", sa.Integer()))
    op.add_column("ai_plan_steps", sa.Column("content_snapshot", postgresql.JSONB()))
    op.add_column("plan_draft_steps", sa.Column("content_version", sa.Integer(), nullable=False))
    op.add_column("plan_draft_steps", sa.Column("content_snapshot", postgresql.JSONB(), nullable=False))
    op.create_foreign_key("fk_ai_plan_steps_content", "ai_plan_steps", "content_library",
        ["exercise_id", "content_version"], ["exercise_id", "content_version"], ondelete="RESTRICT", match="FULL")
    op.create_foreign_key("fk_plan_draft_steps_content", "plan_draft_steps", "content_library",
        ["exercise_id", "content_version"], ["exercise_id", "content_version"], ondelete="RESTRICT")
    op.create_foreign_key("fk_user_events_content", "user_events", "content_library",
        ["exercise_id", "content_version"], ["exercise_id", "content_version"], ondelete="RESTRICT", match="FULL")
    # Keep the B1 validator, changing its content lookup to exact step identity.
    definition = bind.execute(sa.text("SELECT pg_get_functiondef('ly_validate_user_event_catalogue()'::regprocedure)")).scalar_one()
    definition = definition.replace("LEFT JOIN content_library c ON c.id = s.exercise_id", "LEFT JOIN content_library c ON c.exercise_id = s.exercise_id AND c.content_version = s.content_version")
    op.execute(definition)
    op.create_index("ix_content_library_eligible", "content_library", ["mechanic", "exercise_id", "content_version"],
        postgresql_where=sa.text("is_active AND (NOT review_required OR review_status='approved')"))
    op.execute("""
        CREATE FUNCTION ly_preserve_released_content() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF (to_jsonb(NEW) - ARRAY['is_active','review_status','review_evidence'])
             IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['is_active','review_status','review_evidence']) THEN
            RAISE EXCEPTION 'released content is immutable; publish a new content_version';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER tr_content_library_immutable BEFORE UPDATE ON content_library
          FOR EACH ROW EXECUTE FUNCTION ly_preserve_released_content();
        CREATE FUNCTION ly_reject_content_deletion() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'released content cannot be deleted';
        END $$;
        CREATE TRIGGER tr_content_library_no_delete BEFORE DELETE ON content_library
          FOR EACH ROW EXECUTE FUNCTION ly_reject_content_deletion();
        CREATE FUNCTION ly_preserve_step_content() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF ROW(NEW.exercise_id, NEW.content_version, NEW.content_snapshot, NEW.title, NEW.description, NEW.mechanic)
             IS DISTINCT FROM ROW(OLD.exercise_id, OLD.content_version, OLD.content_snapshot, OLD.title, OLD.description, OLD.mechanic) THEN
            RAISE EXCEPTION 'plan content snapshot is immutable';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER tr_ai_plan_steps_content BEFORE UPDATE ON ai_plan_steps
          FOR EACH ROW EXECUTE FUNCTION ly_preserve_step_content();
        CREATE FUNCTION ly_preserve_draft_content() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF ROW(NEW.exercise_id, NEW.content_version, NEW.content_snapshot, NEW.mechanic)
             IS DISTINCT FROM ROW(OLD.exercise_id, OLD.content_version, OLD.content_snapshot, OLD.mechanic) THEN
            RAISE EXCEPTION 'draft content snapshot is immutable';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER tr_plan_draft_steps_content BEFORE UPDATE ON plan_draft_steps
          FOR EACH ROW EXECUTE FUNCTION ly_preserve_draft_content();
    """)


def downgrade():
    raise RuntimeError("WP-03.1 cutover discards old-model data; use forward repair or a verified pre-rollout backup")
