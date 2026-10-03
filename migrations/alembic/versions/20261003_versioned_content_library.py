"""WP-03.1 append-only release authority; retain original legacy identities."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261003_content_library"
down_revision = "20260905_event_privacy"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    # Renaming preserves the old single-ID FKs and all original payload bytes.
    op.rename_table("content_library", "legacy_content_library")
    op.execute("ALTER TABLE legacy_content_library RENAME CONSTRAINT uq_content_library_identity TO uq_legacy_content_library_identity")
    op.create_table(
        "content_library",
        sa.Column("exercise_id", sa.Text(), primary_key=True),
        sa.Column("content_version", sa.Integer(), primary_key=True),
        sa.Column("display", postgresql.JSONB(), nullable=False),
        sa.Column("duration_seconds", sa.Integer()),
        sa.Column("mechanic", sa.Text()),
        sa.Column("modality", sa.Text()),
        sa.Column("requirements", postgresql.JSONB(), nullable=False),
        sa.Column("cooldown_days", sa.Integer()),
        sa.Column("review_required", sa.Boolean(), nullable=False),
        sa.Column("review_status", sa.Text(), nullable=False),
        sa.Column("review_evidence", postgresql.JSONB()),
        sa.Column("media", postgresql.JSONB()),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("legacy_record", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("content_version > 0", name="ck_content_version_positive"),
        sa.CheckConstraint("review_status IN ('unreviewed','approved','rejected')", name="ck_content_review_status"),
        sa.CheckConstraint("legacy_record OR (duration_seconds IS NOT NULL AND duration_seconds > 0 AND cooldown_days IS NOT NULL AND cooldown_days >= 0 AND mechanic IS NOT NULL AND mechanic IN ('switch','unload') AND modality IS NOT NULL)", name="ck_content_protocol"),
    )
    op.execute("""
        INSERT INTO content_library (exercise_id, content_version, display,
          requirements, review_required, review_status, is_active, legacy_record)
        SELECT id, content_version, content_payload, '{}', false, 'unreviewed', false, true
        FROM legacy_content_library;
    """)
    for table in ("ai_plan_steps", "plan_draft_steps"):
        op.add_column(table, sa.Column("content_version", sa.Integer()))
        op.add_column(table, sa.Column("content_snapshot", postgresql.JSONB()))
    op.execute("""
        UPDATE ai_plan_steps s SET content_version=c.content_version
        FROM legacy_content_library c WHERE s.exercise_id=c.id;
    """)
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    op.drop_constraint("fk_ai_plan_steps_exercise_id", "ai_plan_steps", type_="foreignkey")
    op.create_foreign_key("fk_ai_plan_steps_content", "ai_plan_steps", "content_library",
        ["exercise_id", "content_version"], ["exercise_id", "content_version"], ondelete="RESTRICT", match="FULL")
    # A pre-existing JSON-sourced draft has no proven version: keep NULL rather
    # than inventing an equivalence. Activation now fails closed for that draft.
    op.create_foreign_key("fk_plan_draft_steps_content", "plan_draft_steps", "content_library",
        ["exercise_id", "content_version"], ["exercise_id", "content_version"], ondelete="RESTRICT")
    # Event identity already had a composite link. Repoint it without rewriting events.
    bind = op.get_bind()
    constraints = sa.inspect(bind).get_foreign_keys("user_events")
    for fk in constraints:
        if fk["constrained_columns"] == ["exercise_id", "content_version"]:
            op.drop_constraint(fk["name"], "user_events", type_="foreignkey")
    op.create_foreign_key("fk_user_events_content", "user_events", "content_library",
        ["exercise_id", "content_version"], ["exercise_id", "content_version"], ondelete="RESTRICT", match="FULL")
    # Preserve the complete B1 catalogue validator, changing only its content
    # lookup to the immutable step identity (latest-version joins are unsafe).
    definition = bind.execute(sa.text("SELECT pg_get_functiondef('ly_validate_user_event_catalogue()'::regprocedure)")).scalar_one()
    definition = definition.replace("LEFT JOIN content_library c ON c.id = s.exercise_id", "LEFT JOIN content_library c ON c.exercise_id = s.exercise_id AND c.content_version = s.content_version")
    op.execute(definition)
    op.create_index("ix_content_library_eligible", "content_library", ["mechanic", "exercise_id", "content_version"],
        postgresql_where=sa.text("NOT legacy_record AND is_active AND (NOT review_required OR review_status='approved')"))
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
    raise RuntimeError("WP-03.1 downgrade would erase released versions/history; use forward repair or a verified pre-rollout backup")
