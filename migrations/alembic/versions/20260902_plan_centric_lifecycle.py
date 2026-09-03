"""Implement the plan-centric lifecycle authority.

Revision ID: 20260902_plan_lifecycle
Revises: 20260827_schema_baseline
Create Date: 2026-09-02

The revision is intentionally transactional. Ambiguous legacy rows abort the
upgrade instead of inventing activation, terminal, abandonment, or chronology
facts. ``apscheduler_jobs`` remains outside Alembic ownership.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260902_plan_lifecycle"
down_revision = "20260827_schema_baseline"
branch_labels = None
depends_on = None


def _reject_ambiguous_legacy_rows() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM ai_plans
            WHERE status IS NULL
               OR status::text NOT IN ('active','paused','completed','abandoned')
          ) THEN
            RAISE EXCEPTION 'WP-01.3: unproven or unsupported plan status';
          END IF;

          IF EXISTS (
            SELECT user_id FROM ai_plans
            WHERE status::text IN ('active','paused')
            GROUP BY user_id HAVING count(*) > 1
          ) THEN
            RAISE EXCEPTION 'WP-01.3: multiple current plans require evidence-based remediation';
          END IF;

          IF EXISTS (
            SELECT 1 FROM users u
            WHERE u.current_state IS NULL
              AND NOT EXISTS (SELECT 1 FROM ai_plans p WHERE p.user_id = u.id)
          ) THEN
            RAISE EXCEPTION 'WP-01.3: onboarding progress cannot be proven';
          END IF;

          IF EXISTS (
            SELECT 1 FROM ai_plan_steps
            WHERE (is_completed IS TRUE AND skipped IS TRUE)
               OR (
                 is_completed IS TRUE
                 AND lower(step_status) IN ('skipped','expired','canceled')
               )
               OR (
                 skipped IS TRUE
                 AND lower(step_status) IN ('completed','expired','canceled')
               )
          ) THEN
            RAISE EXCEPTION 'WP-01.3: contradictory legacy step terminal facts';
          END IF;

          IF EXISTS (
            SELECT 1 FROM ai_plan_steps
            WHERE step_status IS NULL
               OR lower(step_status) NOT IN
                  ('pending','delivered','completed','skipped','expired','canceled')
          ) THEN
            RAISE EXCEPTION 'WP-01.3: unsupported legacy step status';
          END IF;

          IF EXISTS (SELECT 1 FROM ai_plan_days WHERE day_number IS NULL OR day_number <= 0) THEN
            RAISE EXCEPTION 'WP-01.3: invalid legacy plan day number';
          END IF;

          IF EXISTS (
            SELECT plan_id, day_number FROM ai_plan_days
            GROUP BY plan_id, day_number HAVING count(*) > 1
          ) THEN
            RAISE EXCEPTION 'WP-01.3: duplicate legacy plan day';
          END IF;

          IF EXISTS (
            SELECT 1 FROM ai_plan_steps WHERE order_in_day IS NULL
          ) OR EXISTS (
            SELECT day_id, order_in_day FROM ai_plan_steps
            GROUP BY day_id, order_in_day HAVING count(*) > 1
          ) THEN
            RAISE EXCEPTION 'WP-01.3: missing or duplicate legacy step order';
          END IF;
        END $$;
        """
    )


def _backfill_evidence() -> None:
    op.execute(
        """
        UPDATE ai_plans AS plan
        SET total_days = evidence.day_count
        FROM (
          SELECT plan_id, max(day_number) AS day_count
          FROM ai_plan_days
          GROUP BY plan_id
        ) AS evidence
        WHERE plan.id = evidence.plan_id
          AND plan.total_days IS NULL
          AND evidence.day_count IN (7, 14)
        """
    )
    op.execute(
        """
        UPDATE ai_plans
        SET activated_at = COALESCE(start_date, created_at)
        WHERE COALESCE(start_date, created_at) IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE ai_plans
        SET abandoned_at = end_date
        WHERE status::text = 'abandoned'
          AND abandoned_at IS NULL
          AND end_date IS NOT NULL
        """
    )
    op.execute(
        """
        WITH numbered AS (
          SELECT id,
                 row_number() OVER (
                   PARTITION BY user_id
                   ORDER BY COALESCE(activated_at, start_date, created_at), id
                 ) AS cycle_number
          FROM ai_plans
        )
        UPDATE ai_plans AS plan
        SET cycle_number = numbered.cycle_number
        FROM numbered
        WHERE plan.id = numbered.id
        """
    )
    op.execute(
        """
        UPDATE ai_plan_steps
        SET step_status = CASE
              WHEN is_completed IS TRUE THEN 'completed'
              WHEN skipped IS TRUE THEN 'skipped'
              ELSE lower(step_status)
            END,
            terminal_at = CASE
              WHEN is_completed IS TRUE THEN completed_at
              WHEN lower(step_status) = 'completed' THEN completed_at
              WHEN lower(step_status) = 'expired' THEN expires_at
              ELSE terminal_at
            END
        """
    )
    op.execute(
        """
        UPDATE ai_plan_steps
        SET version = CASE
          WHEN step_status IN ('completed','skipped','expired','canceled')
               AND terminal_at IS NULL THEN 0
          ELSE 1
        END
        """
    )
    op.execute(
        """
        INSERT INTO onboarding_progress (
          user_id, stage, started_at, completed_at, updated_at
        )
        SELECT u.id,
               CASE
                 WHEN EXISTS (SELECT 1 FROM ai_plans p WHERE p.user_id = u.id)
                   THEN 'COMPLETED'
                 WHEN u.current_state = 'IDLE_NEW' THEN 'START'
                 WHEN u.current_state LIKE 'ONBOARDING:%' THEN
                   COALESCE(NULLIF(split_part(u.current_state, ':', 2), ''), 'START')
                 ELSE 'COMPLETED'
               END,
               u.created_at,
               NULL,
               now()
        FROM users u
        ON CONFLICT (user_id) DO NOTHING
        """
    )

    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM ai_plans
            WHERE total_days IS NULL OR total_days NOT IN (7,14)
               OR activated_at IS NULL
               OR (status::text = 'abandoned' AND abandoned_at IS NULL)
               OR (abandoned_at IS NOT NULL AND abandoned_at < activated_at)
          ) THEN
            RAISE EXCEPTION 'WP-01.3: plan chronology or duration cannot be proven';
          END IF;

          IF EXISTS (
            SELECT 1
            FROM ai_plans p
            LEFT JOIN ai_plan_days d ON d.plan_id = p.id
            GROUP BY p.id, p.total_days
            HAVING count(d.id) <> p.total_days
               OR min(d.day_number) <> 1
               OR max(d.day_number) <> p.total_days
          ) THEN
            RAISE EXCEPTION 'WP-01.3: plan/day aggregate is structurally incomplete';
          END IF;

          IF EXISTS (
            SELECT 1
            FROM ai_plan_days d
            LEFT JOIN ai_plan_steps s ON s.day_id = d.id
            GROUP BY d.id
            HAVING count(s.id) = 0
          ) THEN
            RAISE EXCEPTION 'WP-01.3: plan day has no steps';
          END IF;

          IF EXISTS (
            SELECT 1 FROM ai_plan_steps
            WHERE scheduled_for IS NOT NULL AND expires_at IS NOT NULL
              AND expires_at <= scheduled_for
          ) THEN
            RAISE EXCEPTION 'WP-01.3: invalid step schedule chronology';
          END IF;

          IF EXISTS (
            SELECT 1
            FROM ai_plans p
            JOIN ai_plan_days d ON d.plan_id = p.id
            JOIN ai_plan_steps s ON s.day_id = d.id
            WHERE p.status::text IN ('completed','abandoned')
              AND s.step_status NOT IN ('completed','skipped','expired','canceled')
          ) THEN
            RAISE EXCEPTION 'WP-01.3: terminal plan contains an open step';
          END IF;
        END $$;
        """
    )


def _create_aggregate_constraint_triggers() -> None:
    op.execute(
        """
        CREATE FUNCTION ly_check_plan_aggregate(p_plan_id integer)
        RETURNS void LANGUAGE plpgsql AS $$
        DECLARE
          expected_days integer;
          actual_days integer;
          plan_status_value text;
        BEGIN
          SELECT total_days, status::text INTO expected_days, plan_status_value
          FROM ai_plans WHERE id = p_plan_id;
          IF NOT FOUND THEN
            RETURN;
          END IF;
          SELECT count(*) INTO actual_days FROM ai_plan_days WHERE plan_id = p_plan_id;
          IF actual_days <> expected_days THEN
            RAISE EXCEPTION 'plan % must contain exactly % days, found %',
              p_plan_id, expected_days, actual_days;
          END IF;
          IF EXISTS (
            SELECT 1 FROM ai_plan_days d
            WHERE d.plan_id = p_plan_id
              AND (d.day_number < 1 OR d.day_number > expected_days)
          ) THEN
            RAISE EXCEPTION 'plan % contains an out-of-range day', p_plan_id;
          END IF;
          IF EXISTS (
            SELECT 1 FROM ai_plan_days d
            LEFT JOIN ai_plan_steps s ON s.day_id = d.id
            WHERE d.plan_id = p_plan_id
            GROUP BY d.id HAVING count(s.id) = 0
          ) THEN
            RAISE EXCEPTION 'plan % contains a day without steps', p_plan_id;
          END IF;
          IF plan_status_value IN ('completed','abandoned') AND EXISTS (
            SELECT 1 FROM ai_plan_days d
            JOIN ai_plan_steps s ON s.day_id = d.id
            WHERE d.plan_id = p_plan_id
              AND s.step_status NOT IN ('completed','skipped','expired','canceled')
          ) THEN
            RAISE EXCEPTION 'terminal plan % contains an open step', p_plan_id;
          END IF;
        END $$;

        CREATE FUNCTION ly_check_plan_from_plan_row()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          PERFORM ly_check_plan_aggregate(CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END);
          RETURN NULL;
        END $$;

        CREATE FUNCTION ly_check_plan_from_day_row()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP IN ('UPDATE','DELETE') THEN
            PERFORM ly_check_plan_aggregate(OLD.plan_id);
          END IF;
          IF TG_OP IN ('INSERT','UPDATE') THEN
            PERFORM ly_check_plan_aggregate(NEW.plan_id);
          END IF;
          RETURN NULL;
        END $$;

        CREATE FUNCTION ly_check_plan_from_step_row()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          old_plan_id integer;
          new_plan_id integer;
        BEGIN
          IF TG_OP IN ('UPDATE','DELETE') THEN
            SELECT plan_id INTO old_plan_id FROM ai_plan_days WHERE id = OLD.day_id;
            PERFORM ly_check_plan_aggregate(old_plan_id);
          END IF;
          IF TG_OP IN ('INSERT','UPDATE') THEN
            SELECT plan_id INTO new_plan_id FROM ai_plan_days WHERE id = NEW.day_id;
            PERFORM ly_check_plan_aggregate(new_plan_id);
          END IF;
          RETURN NULL;
        END $$;

        CREATE CONSTRAINT TRIGGER ct_ai_plans_aggregate_exists
          AFTER INSERT OR UPDATE OR DELETE ON ai_plans
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION ly_check_plan_from_plan_row();
        CREATE CONSTRAINT TRIGGER ct_ai_plan_days_aggregate_exists
          AFTER INSERT OR UPDATE OR DELETE ON ai_plan_days
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION ly_check_plan_from_day_row();
        CREATE CONSTRAINT TRIGGER ct_ai_plan_steps_aggregate_exists
          AFTER INSERT OR UPDATE OR DELETE ON ai_plan_steps
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION ly_check_plan_from_step_row();
        """
    )


def upgrade() -> None:
    bind = op.get_bind()
    # Legacy ``activated_at`` is timestamp-without-time-zone and therefore is
    # not trusted as chronology evidence. Backfill uses timezone-aware
    # start/creation facts; UTC keeps their temporary cast and the final type
    # conversion deterministic even if the migration session has another zone.
    op.execute("SET LOCAL TIME ZONE 'UTC'")
    op.execute("ALTER TYPE plan_status RENAME TO legacy_plan_status")
    plan_status = postgresql.ENUM(
        "active", "paused", "completed", "abandoned", name="plan_status"
    )
    plan_step_status = postgresql.ENUM(
        "pending",
        "delivered",
        "completed",
        "skipped",
        "expired",
        "canceled",
        name="plan_step_status",
    )
    plan_status.create(bind, checkfirst=False)
    plan_step_status.create(bind, checkfirst=False)

    op.create_table(
        "onboarding_progress",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("length(btrim(stage)) > 0", name="ck_onboarding_progress_stage"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )

    op.add_column("ai_plans", sa.Column("cycle_number", sa.Integer(), nullable=True))
    op.add_column("ai_plans", sa.Column("abandoned_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "ai_plans", sa.Column("version", sa.Integer(), server_default="1", nullable=False)
    )
    op.add_column("ai_plan_steps", sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "ai_plan_steps", sa.Column("version", sa.Integer(), server_default="1", nullable=False)
    )

    _reject_ambiguous_legacy_rows()
    _backfill_evidence()

    # Compatibility columns remain physically present but inert. Evidence has
    # already been copied into authoritative facts, so mirrors are cleared and
    # their defaults removed rather than maintained as a second state machine.
    op.execute("ALTER TABLE users ALTER COLUMN current_state DROP DEFAULT")
    op.execute("UPDATE users SET current_state = NULL, plan_end_date = NULL")
    op.execute("ALTER TABLE user_profiles ALTER COLUMN is_paused DROP DEFAULT")
    op.alter_column("user_profiles", "is_paused", nullable=True)
    op.execute("UPDATE user_profiles SET is_paused = NULL")
    op.execute("ALTER TABLE ai_plans ALTER COLUMN current_mode DROP DEFAULT")
    op.execute("ALTER TABLE ai_plans ALTER COLUMN current_day DROP DEFAULT")
    op.alter_column("ai_plans", "current_day", nullable=True)
    op.execute(
        "UPDATE ai_plans SET current_mode = NULL, current_day = NULL, end_date = NULL"
    )
    op.execute("UPDATE ai_plan_days SET is_completed = NULL, completed_at = NULL")
    op.execute("ALTER TABLE ai_plan_days ALTER COLUMN is_completed DROP DEFAULT")
    op.execute(
        "UPDATE ai_plan_steps "
        "SET is_completed = NULL, skipped = NULL, completed_at = NULL"
    )
    op.execute("ALTER TABLE ai_plan_steps ALTER COLUMN is_completed DROP DEFAULT")
    op.execute("ALTER TABLE ai_plan_steps ALTER COLUMN skipped DROP DEFAULT")

    op.execute("ALTER TABLE ai_plans ALTER COLUMN status DROP DEFAULT")
    op.execute(
        "ALTER TABLE ai_plans ALTER COLUMN status TYPE plan_status "
        "USING status::text::plan_status"
    )
    op.execute("ALTER TABLE ai_plans ALTER COLUMN status SET DEFAULT 'active'::plan_status")
    op.alter_column("ai_plans", "status", nullable=False)
    op.alter_column("ai_plans", "cycle_number", nullable=False)
    op.alter_column("ai_plans", "total_days", nullable=False)
    op.execute(
        "ALTER TABLE ai_plans ALTER COLUMN activated_at TYPE timestamptz "
        "USING activated_at AT TIME ZONE 'UTC'"
    )
    op.alter_column("ai_plans", "activated_at", nullable=False)

    op.execute("ALTER TABLE ai_plan_steps ALTER COLUMN step_status DROP DEFAULT")
    op.execute(
        "ALTER TABLE ai_plan_steps ALTER COLUMN step_status TYPE plan_step_status "
        "USING step_status::text::plan_step_status"
    )
    op.execute(
        "ALTER TABLE ai_plan_steps ALTER COLUMN step_status "
        "SET DEFAULT 'pending'::plan_step_status"
    )

    op.drop_constraint("ai_plans_total_days_canonical_check", "ai_plans", type_="check")
    op.create_check_constraint("ck_ai_plans_cycle_number", "ai_plans", "cycle_number > 0")
    op.create_check_constraint("ck_ai_plans_total_days", "ai_plans", "total_days IN (7,14)")
    op.create_check_constraint("ck_ai_plans_version", "ai_plans", "version > 0")
    op.create_check_constraint(
        "ck_ai_plans_chronology",
        "ai_plans",
        "activated_at IS NOT NULL AND "
        "(status <> 'abandoned' OR abandoned_at IS NOT NULL) AND "
        "(abandoned_at IS NULL OR abandoned_at >= activated_at)",
    )
    op.create_unique_constraint(
        "uq_ai_plans_user_cycle", "ai_plans", ["user_id", "cycle_number"]
    )
    op.create_index(
        "ux_ai_plans_one_current_per_user",
        "ai_plans",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('active','paused')"),
    )
    op.create_index(
        "ix_ai_plans_user_created",
        "ai_plans",
        ["user_id", sa.text("created_at DESC")],
    )
    op.create_check_constraint("ck_ai_plan_days_day_number", "ai_plan_days", "day_number > 0")
    op.create_unique_constraint(
        "uq_ai_plan_days_plan_day", "ai_plan_days", ["plan_id", "day_number"]
    )
    op.create_unique_constraint(
        "uq_ai_plan_steps_day_order", "ai_plan_steps", ["day_id", "order_in_day"]
    )
    op.create_check_constraint("ck_ai_plan_steps_version", "ai_plan_steps", "version >= 0")
    op.create_check_constraint(
        "ck_ai_plan_steps_schedule_chronology",
        "ai_plan_steps",
        "scheduled_for IS NULL OR expires_at IS NULL OR expires_at > scheduled_for",
    )
    op.create_check_constraint(
        "ck_ai_plan_steps_terminal_timestamp",
        "ai_plan_steps",
        "(version = 0 AND "
        "step_status IN ('completed','skipped','expired','canceled') AND "
        "terminal_at IS NULL) OR "
        "(version > 0 AND "
        "((step_status IN ('completed','skipped','expired','canceled')) = "
        "(terminal_at IS NOT NULL)))",
    )

    op.create_table(
        "plan_lifecycle_operations",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("plan_step_id", sa.Integer(), nullable=True),
        sa.Column("source_operation_id", sa.String(length=160), nullable=False),
        sa.Column("operation", sa.String(length=40), nullable=False),
        sa.Column("result_status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("length(btrim(operation)) > 0", name="ck_plan_lifecycle_operation_name"),
        sa.CheckConstraint("length(btrim(result_status)) > 0", name="ck_plan_lifecycle_result"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["plan_id"], ["ai_plans.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["plan_step_id"], ["ai_plan_steps.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "source_operation_id", name="uq_plan_lifecycle_operation_source"
        ),
    )
    _create_aggregate_constraint_triggers()


def downgrade() -> None:
    raise RuntimeError(
        "The plan-centric lifecycle migration is a forward authority switch and cannot be downgraded."
    )
