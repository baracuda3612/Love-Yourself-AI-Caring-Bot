"""Persist the current explicit menu text input."""

from alembic import op
import sqlalchemy as sa

revision = "20261104_menu_input"
down_revision = "20261104_onboarding_controls"
branch_labels = None
depends_on = None


_FEEDBACK_TARGET = (
    "(source = 'exercise_efficacy' AND plan_step_id IS NOT NULL "
    "AND coach_message_id IS NULL AND source_message_id IS NULL{no_direct}) OR "
    "(source = 'coach_quality' AND plan_step_id IS NULL "
    "AND coach_message_id IS NOT NULL AND source_message_id IS NULL{no_direct}) OR "
    "(source = 'product_feedback' AND plan_step_id IS NULL AND coach_message_id IS NULL "
    "AND ({product_target}))"
)
_OWNERSHIP = """CREATE OR REPLACE FUNCTION ly_validate_feedback_target_ownership()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NOT ly_event_json_is_safe(NEW.context) THEN
            RAISE EXCEPTION 'feedback context violates bounded privacy rules';
          END IF;
          IF NEW.source = 'exercise_efficacy' AND NOT EXISTS (
            SELECT 1
            FROM ai_plan_steps s
            JOIN ai_plan_days d ON d.id = s.day_id
            JOIN ai_plans p ON p.id = d.plan_id
            WHERE s.id = NEW.plan_step_id
              AND s.step_status = 'completed'
              AND p.user_id = NEW.user_id
          ) THEN
            RAISE EXCEPTION 'exercise feedback target is not a completed step owned by user';
          END IF;
          IF NEW.source = 'coach_quality' AND NOT EXISTS (
            SELECT 1 FROM chat_history c
            WHERE c.id = NEW.coach_message_id
              AND c.user_id = NEW.user_id
              AND c.role = 'assistant'
          ) THEN
            RAISE EXCEPTION 'coach feedback target is not an assistant message owned by user';
          END IF;
          IF NEW.source = 'product_feedback' {source_guard} AND NOT EXISTS (
            SELECT 1 FROM chat_history c
            WHERE c.id = NEW.source_message_id
              AND c.user_id = NEW.user_id
              AND c.role = 'user'
          ) THEN
            RAISE EXCEPTION 'product feedback source is not a user message owned by user';
          END IF;
          {direct_check}
          RETURN NEW;
        END $$;
"""


def _feedback_constraints(*, direct):
    source_guard = "AND NEW.telegram_message_id IS NULL" if direct else ""
    direct_check = """
          IF NEW.telegram_message_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM users u WHERE u.id = NEW.user_id AND NEW.source_operation_id =
              concat('telegram:feedback:', u.tg_id, ':', NEW.telegram_message_id)
          ) THEN
            RAISE EXCEPTION 'direct feedback source is not bound to the owning Telegram actor';
          END IF;
    """ if direct else ""
    op.execute(_OWNERSHIP.format(
        source_guard=source_guard,
        direct_check=direct_check,
    ))
    op.execute("DROP TRIGGER tr_feedback_events_validate_target_ownership ON feedback_events")
    direct_columns = ", telegram_message_id, source_operation_id" if direct else ""
    op.execute(
        "CREATE TRIGGER tr_feedback_events_validate_target_ownership "
        "BEFORE INSERT OR UPDATE OF user_id, source, plan_step_id, coach_message_id, "
        f"source_message_id, context{direct_columns} ON feedback_events "
        "FOR EACH ROW EXECUTE FUNCTION ly_validate_feedback_target_ownership()"
    )
    op.drop_constraint("ck_feedback_events_target", "feedback_events", type_="check")
    product_target = "source_message_id IS NOT NULL"
    if direct:
        product_target = (
            "(source_message_id IS NOT NULL AND telegram_message_id IS NULL) OR "
            "(source_message_id IS NULL AND telegram_message_id IS NOT NULL "
            "AND telegram_message_id > 0 AND extracted_text IS NOT NULL "
            "AND length(btrim(extracted_text)) > 0)"
        )
    op.create_check_constraint(
        "ck_feedback_events_target", "feedback_events",
        _FEEDBACK_TARGET.format(
            no_direct=" AND telegram_message_id IS NULL" if direct else "",
            product_target=product_target,
        ),
    )


def upgrade():
    op.add_column("feedback_events", sa.Column("telegram_message_id", sa.BigInteger()))
    _feedback_constraints(direct=True)
    op.create_table(
        "menu_input",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("action", sa.String(16), nullable=True),
        sa.Column("nonce", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("enrollment_id", sa.BigInteger(),
                  sa.ForeignKey("deployment_enrollments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("notice_id", sa.BigInteger(), nullable=False),
        sa.Column("draft_days", sa.Integer(), nullable=True),
        sa.CheckConstraint("draft_days BETWEEN 0 AND 127", name="ck_menu_input_draft_days"),
        sa.Column("target_plan_id", sa.Integer(), nullable=True),
        sa.Column("target_version", sa.Integer(), nullable=True),
        sa.Column("last_message_id", sa.BigInteger(), nullable=False),
    )


def downgrade():
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM feedback_events WHERE telegram_message_id IS NOT NULL) THEN
            RAISE EXCEPTION 'direct feedback exists; disposition required before downgrade';
          END IF;
        END $$;
    """)
    _feedback_constraints(direct=False)
    op.drop_column("feedback_events", "telegram_message_id")
    op.drop_table("menu_input")
