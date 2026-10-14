"""WP-03.5 plan-scoped completion obligation and continuation identity."""
from alembic import op
import sqlalchemy as sa

revision = '20261014_plan_completion'
down_revision = '20261010_scheduled_delivery'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'plan_completions',
        sa.Column('plan_id', sa.Integer(), sa.ForeignKey('ai_plans.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('successor_plan_id', sa.Integer(), sa.ForeignKey('ai_plans.id', ondelete='CASCADE'), nullable=False),
        sa.Column('report_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('state', sa.String(32), nullable=False),
        sa.Column('wake_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True)),
        sa.Column('payload', sa.Text()),
        sa.Column('chat_id', sa.BigInteger()),
        sa.Column('message_id', sa.BigInteger()),
        sa.Column('sent_at', sa.DateTime(timezone=True)),
        sa.Column('failure_code', sa.String(64)),
        sa.Column('schedule_pending', sa.Boolean(), nullable=False),
        sa.Column('release_started_at', sa.DateTime(timezone=True)),
        sa.Column('release_payload', sa.Text()),
        sa.Column('release_message_id', sa.BigInteger()),
        sa.Column('released_at', sa.DateTime(timezone=True)),
        sa.CheckConstraint("state <> 'released' OR (release_message_id > 0 AND release_payload IS NOT NULL AND released_at IS NOT NULL)", name='ck_plan_completion_release'),
        sa.UniqueConstraint('successor_plan_id', name='uq_plan_completion_successor'),
        sa.CheckConstraint('plan_id <> successor_plan_id', name='ck_plan_completion_distinct'),
        sa.CheckConstraint("state IN ('pending','in_flight','uncertain','sent','failed','release_in_flight','release_uncertain','released','sent_held')", name='ck_plan_completion_state'),
        sa.CheckConstraint('attempts BETWEEN 0 AND 3', name='ck_plan_completion_attempts'),
        sa.CheckConstraint("state NOT IN ('sent','sent_held') OR (message_id > 0 AND chat_id IS NOT NULL AND sent_at IS NOT NULL AND payload IS NOT NULL)", name='ck_plan_completion_receipt'),
    )
    op.create_index('ix_plan_completions_work', 'plan_completions', ['state', 'wake_at'], postgresql_where=sa.text("state IN ('pending','in_flight','release_in_flight') OR schedule_pending"))
    op.execute("""
    CREATE FUNCTION guard_plan_completion() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'UPDATE' THEN
        IF (NEW.plan_id, NEW.successor_plan_id) IS DISTINCT FROM (OLD.plan_id, OLD.successor_plan_id) THEN
          RAISE EXCEPTION 'completion identity is immutable';
        END IF;
        IF OLD.sent_at IS NOT NULL AND
          (NEW.payload, NEW.chat_id, NEW.message_id, NEW.sent_at, NEW.started_at, NEW.attempts)
          IS DISTINCT FROM
          (OLD.payload, OLD.chat_id, OLD.message_id, OLD.sent_at, OLD.started_at, OLD.attempts) THEN
          RAISE EXCEPTION 'completion receipt is immutable';
        END IF;
        IF OLD.state = 'released' AND
          (NEW.state, NEW.release_started_at, NEW.release_payload, NEW.release_message_id, NEW.released_at)
          IS DISTINCT FROM
          (OLD.state, OLD.release_started_at, OLD.release_payload, OLD.release_message_id, OLD.released_at) THEN
          RAISE EXCEPTION 'continuation release receipt is immutable';
        END IF;
      END IF;
      IF NOT EXISTS (
        SELECT 1 FROM ai_plans source JOIN ai_plans successor ON successor.id = NEW.successor_plan_id
        WHERE source.id = NEW.plan_id AND source.status = 'completed' AND source.user_id = successor.user_id
      ) THEN RAISE EXCEPTION 'completion requires owned completed source and successor'; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER plan_completion_guard BEFORE INSERT OR UPDATE ON plan_completions
      FOR EACH ROW EXECUTE FUNCTION guard_plan_completion();
    """)


def downgrade():
    op.drop_table('plan_completions')
    op.execute('DROP FUNCTION guard_plan_completion()')
