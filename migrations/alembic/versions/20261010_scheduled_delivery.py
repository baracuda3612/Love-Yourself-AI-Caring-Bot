"""WP-03.4 scheduled attempts and completed-only efficacy feedback."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '20261010_scheduled_delivery'
down_revision = '20261003_content_library'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'exercise_deliveries',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('plan_step_id', sa.Integer(), sa.ForeignKey('ai_plan_steps.id', ondelete='CASCADE'), nullable=False),
        sa.Column('source_operation_id', sa.String(160), nullable=False),
        sa.Column('attempt', sa.Integer(), nullable=False),
        sa.Column('state', sa.String(32), nullable=False),
        sa.Column('presentation_snapshot', postgresql.JSONB(), nullable=False),
        sa.Column('rendered_payload', sa.Text()),
        sa.Column('variant', sa.String(8)),
        sa.Column('chat_id', sa.BigInteger(), nullable=False),
        sa.Column('message_id', sa.BigInteger()),
        sa.Column('failure_code', sa.String(64)),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('confirmed_at', sa.DateTime(timezone=True)),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True)),
        sa.Column('visible_status', sa.String(32)),
        sa.Column('visible_feedback', sa.String(64)),
        sa.UniqueConstraint('source_operation_id', 'attempt', name='uq_exercise_delivery_attempt'),
        sa.CheckConstraint('attempt BETWEEN 1 AND 3', name='ck_exercise_delivery_attempt'),
        sa.CheckConstraint("state IN ('in_flight','uncertain','delivered','retryable','terminal_failure')", name='ck_exercise_delivery_state'),
        sa.CheckConstraint("state <> 'delivered' OR (variant IS NOT NULL AND variant IN ('gif','text') AND message_id IS NOT NULL AND message_id > 0 AND rendered_payload IS NOT NULL AND length(rendered_payload) > 0 AND confirmed_at IS NOT NULL)", name='ck_exercise_delivery_receipt'),
        sa.CheckConstraint("source_operation_id = 'scheduler:delivery:' || plan_step_id::text", name='ck_exercise_delivery_source'),
    )
    op.create_index('ux_exercise_deliveries_one_sent', 'exercise_deliveries', ['plan_step_id'], unique=True, postgresql_where=sa.text("state = 'delivered'"))
    op.create_index('ux_exercise_deliveries_one_open', 'exercise_deliveries', ['plan_step_id'], unique=True, postgresql_where=sa.text("state IN ('in_flight','uncertain','delivered')"))
    op.create_index('ix_exercise_deliveries_recovery', 'exercise_deliveries', ['state', 'next_attempt_at'])
    op.create_index('ux_feedback_exercise_plan_step', 'feedback_events', ['user_id', 'plan_step_id'], unique=True, postgresql_where=sa.text("source = 'exercise_efficacy' AND plan_step_id IS NOT NULL"))
    op.create_index('ix_ai_plan_steps_due', 'ai_plan_steps', ['scheduled_for', 'id'], postgresql_where=sa.text("step_status = 'pending'"))
    op.create_index('ix_ai_plan_steps_expiry', 'ai_plan_steps', ['expires_at', 'id'], postgresql_where=sa.text("step_status IN ('pending','delivered')"))
    op.execute("""
    CREATE FUNCTION guard_exercise_delivery() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF (NEW.plan_step_id, NEW.source_operation_id, NEW.attempt,
          NEW.presentation_snapshot, NEW.chat_id, NEW.started_at)
         IS DISTINCT FROM
         (OLD.plan_step_id, OLD.source_operation_id, OLD.attempt,
          OLD.presentation_snapshot, OLD.chat_id, OLD.started_at) THEN
        RAISE EXCEPTION 'exercise delivery identity and snapshot are immutable';
      END IF;
      IF OLD.state = 'delivered' AND
         (NEW.state, NEW.variant, NEW.rendered_payload, NEW.message_id, NEW.confirmed_at)
         IS DISTINCT FROM
         (OLD.state, OLD.variant, OLD.rendered_payload, OLD.message_id, OLD.confirmed_at) THEN
        RAISE EXCEPTION 'confirmed delivery receipt is immutable';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER exercise_delivery_immutable BEFORE UPDATE ON exercise_deliveries
      FOR EACH ROW EXECUTE FUNCTION guard_exercise_delivery();

    CREATE FUNCTION guard_scheduled_efficacy() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.source = 'exercise_efficacy' AND NEW.value NOT IN ('better','same','worse')
        THEN RAISE EXCEPTION 'invalid efficacy value'; END IF;
      IF TG_OP='UPDATE' AND OLD.source='exercise_efficacy' AND NEW IS DISTINCT FROM OLD
        THEN RAISE EXCEPTION 'submitted efficacy is immutable'; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER scheduled_efficacy_guard BEFORE INSERT OR UPDATE ON feedback_events
      FOR EACH ROW EXECUTE FUNCTION guard_scheduled_efficacy();
    """)
    op.execute("""INSERT INTO event_catalog
        (event_name,event_schema_version,event_kind,allowed_property_schema,required_linkage)
        VALUES ('feedback_submitted',1,'user_behavior','{"source":"string","value":"string"}'::jsonb,'["plan_step"]'::jsonb)""")


def downgrade():
    raise RuntimeError('WP-03.4 durable delivery receipts require forward repair')
