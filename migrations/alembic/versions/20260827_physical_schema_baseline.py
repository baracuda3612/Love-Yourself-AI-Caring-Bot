"""Baseline the inspected disposable-testnet application schema.

Revision ID: 20260827_schema_baseline
Revises: None
Create Date: 2026-08-27

`apscheduler_jobs` is intentionally absent. APScheduler owns that table and
recreates it through its SQLAlchemyJobStore lifecycle.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260827_schema_baseline"
down_revision = None
branch_labels = None
depends_on = None


ENUMS = (
    postgresql.ENUM("easy", "medium", "hard", name="difficulty_level"),
    postgresql.ENUM("ACTIVE", "SPORADIC", "RETURNING", "DORMANT", name="engagementstatus"),
    postgresql.ENUM("goal", "preference", "medical", "biography", "insight", name="fact_category"),
    postgresql.ENUM("GOAL", "PREFERENCE", "MEDICAL", "BIOGRAPHY", "INSIGHT", name="factcategory"),
    postgresql.ENUM("burnout_recovery", "sleep_optimization", "digital_detox", name="plan_module"),
    postgresql.ENUM("draft", "active", "paused", "completed", "canceled", name="plan_status"),
    postgresql.ENUM("active", "completed", "paused", "abandoned", name="plan_status_enum"),
    postgresql.ENUM("BURNOUT_RECOVERY", "SLEEP_OPTIMIZATION", "DIGITAL_DETOX", name="planmodule"),
    postgresql.ENUM("user", "assistant", "system", "USER", "ASSISTANT", name="sender_role"),
    postgresql.ENUM("USER", "ASSISTANT", "SYSTEM", name="senderrole"),
    postgresql.ENUM("action", "reflection", "rest", name="step_type"),
    postgresql.ENUM("EDUCATION", "ACTION", "REFLECTION", "REST", name="steptype"),
)

difficulty_level = postgresql.ENUM(
    "easy", "medium", "hard", name="difficulty_level", create_type=False
)
fact_category = postgresql.ENUM(
    "goal", "preference", "medical", "biography", "insight",
    name="fact_category",
    create_type=False,
)
plan_module = postgresql.ENUM(
    "burnout_recovery", "sleep_optimization", "digital_detox",
    name="plan_module",
    create_type=False,
)
plan_status_enum = postgresql.ENUM(
    "active", "completed", "paused", "abandoned",
    name="plan_status_enum",
    create_type=False,
)
step_type = postgresql.ENUM(
    "action", "reflection", "rest", name="step_type", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    for enum in ENUMS:
        enum.create(bind, checkfirst=False)

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tg_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("first_name", sa.String(length=255), nullable=True),
        sa.Column(
            "current_state",
            sa.String(length=255),
            server_default="IDLE_NEW",
            nullable=True,
        ),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column("notification_time", sa.Time(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("plan_end_date", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "current_state IN ("
            "'IDLE_NEW','IDLE_ONBOARDED','IDLE_PLAN_ABORTED','IDLE_FINISHED','IDLE_DROPPED',"
            "'PLAN_FLOW:DATA_COLLECTION','PLAN_FLOW:CONFIRMATION_PENDING','PLAN_FLOW:FINALIZATION',"
            "'ACTIVE','ACTIVE_CONFIRMATION','ACTIVE_PAUSED','ACTIVE_PAUSED_CONFIRMATION',"
            "'ADAPTATION_SELECTION','ADAPTATION_PARAMS','ADAPTATION_CONFIRMATION'"
            ") OR current_state LIKE 'ONBOARDING:%'",
            name="ck_users_current_state",
        ),
        sa.PrimaryKeyConstraint("id", name="users_pkey"),
        sa.UniqueConstraint("tg_id", name="users_tg_id_key"),
    )

    op.create_table(
        "chat_history",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.CheckConstraint("role IN ('user','assistant')", name="chat_history_role_check"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="chat_history_user_id_fkey"),
        sa.PrimaryKeyConstraint("id", name="chat_history_pkey"),
    )
    op.create_index("idx_chat_history_user_id", "chat_history", ["user_id"])

    op.create_table(
        "content_library",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("content_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("internal_name", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("difficulty", sa.Integer(), nullable=False),
        sa.Column("energy_cost", sa.Text(), nullable=False),
        sa.Column("logic_tags", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("content_payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="content_library_pkey"),
    )

    op.create_table(
        "user_profiles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("main_goal", sa.Text(), nullable=True),
        sa.Column("communication_style", sa.Text(), nullable=True),
        sa.Column("name_preference", sa.Text(), nullable=True),
        sa.Column("attributes", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("daily_time_slots", postgresql.JSONB(), nullable=True),
        sa.Column("coach_persona", sa.String(length=20), nullable=True),
        sa.Column("pulse_sent_indices", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=True),
        sa.Column(
            "active_days",
            postgresql.JSONB(),
            server_default=sa.text("'[\"MON\", \"TUE\", \"WED\", \"THU\", \"FRI\"]'::jsonb"),
            nullable=False,
        ),
        sa.Column("is_paused", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("pause_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("evening_slot_collected", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="user_profiles_user_id_fkey", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="user_profiles_pkey"),
        sa.UniqueConstraint("user_id", name="user_profiles_user_id_key"),
    )

    op.create_table(
        "user_facts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("category", fact_category, nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("relevance", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="user_facts_user_id_fkey", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="user_facts_pkey"),
    )
    op.create_index("idx_user_facts_user_category", "user_facts", ["user_id", "category"])

    op.create_table(
        "ai_plans",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column(
            "module_id",
            plan_module,
            server_default=sa.text("'burnout_recovery'::plan_module"),
            nullable=False,
        ),
        sa.Column("goal_description", sa.Text(), nullable=True),
        sa.Column(
            "status",
            plan_status_enum,
            server_default=sa.text("'active'::plan_status_enum"),
            nullable=True,
        ),
        sa.Column("start_date", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("end_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("current_mode", sa.Text(), server_default="standard", nullable=True),
        sa.Column("milestone_status", sa.Text(), server_default="pending", nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("current_day", sa.Integer(), server_default="1", nullable=False),
        sa.Column("duration", sa.String(length=20), nullable=True),
        sa.Column("focus", sa.String(length=20), nullable=True),
        sa.Column("load", sa.String(length=20), nullable=True),
        sa.Column("total_days", sa.Integer(), nullable=True),
        sa.Column(
            "preferred_time_slots",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "total_days IS NULL OR total_days = ANY (ARRAY[7, 14, 21, 90])",
            name="ai_plans_total_days_canonical_check",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_user", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="ai_plans_pkey"),
    )
    op.create_index("ix_ai_plans_user_id", "ai_plans", ["user_id"])

    op.create_table(
        "ai_plan_days",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("day_number", sa.Integer(), nullable=False),
        sa.Column("focus_theme", sa.String(), nullable=True),
        sa.Column("is_completed", sa.Boolean(), server_default=sa.text("false"), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["plan_id"], ["ai_plans.id"], name="fk_plan", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="ai_plan_days_pkey"),
    )
    op.create_index("ix_ai_plan_days_plan_id", "ai_plan_days", ["plan_id"])

    op.create_table(
        "ai_plan_steps",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("day_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("step_type", step_type, server_default=sa.text("'action'::step_type"), nullable=True),
        sa.Column(
            "difficulty",
            difficulty_level,
            server_default=sa.text("'easy'::difficulty_level"),
            nullable=True,
        ),
        sa.Column("order_in_day", sa.Integer(), server_default="0", nullable=True),
        sa.Column("time_of_day", sa.String(), server_default="any", nullable=True),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_completed", sa.Boolean(), server_default=sa.text("false"), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("skipped", sa.Boolean(), server_default=sa.text("false"), nullable=True),
        sa.Column("job_id", sa.String(), nullable=True),
        sa.Column("exercise_id", sa.String(), nullable=True),
        sa.Column("time_slot", sa.String(length=32), nullable=True),
        sa.Column("slot_type", sa.String(length=20), server_default="CORE", nullable=False),
        sa.Column("step_status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tg_message_id", sa.Integer(), nullable=True),
        sa.Column("mechanic", sa.String(length=10), nullable=False),
        sa.CheckConstraint("mechanic IN ('switch','unload')", name="ai_plan_steps_mechanic_check"),
        sa.ForeignKeyConstraint(["day_id"], ["ai_plan_days.id"], name="fk_day", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["exercise_id"],
            ["content_library.id"],
            name="fk_ai_plan_steps_exercise_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="ai_plan_steps_pkey"),
    )
    op.create_index("idx_ai_plan_steps_exercise_id", "ai_plan_steps", ["exercise_id"])
    op.create_index("ix_ai_plan_steps_day_id", "ai_plan_steps", ["day_id"])
    op.create_index("ix_ai_plan_steps_step_status", "ai_plan_steps", ["step_status"])

    op.create_table(
        "ai_plan_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("applied_adaptation_type", sa.String(), nullable=False),
        sa.Column("diff", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.ForeignKeyConstraint(["plan_id"], ["ai_plans.id"], name="ai_plan_versions_plan_id_fkey"),
        sa.PrimaryKeyConstraint("id", name="ai_plan_versions_pkey"),
    )
    op.create_index("ix_ai_plan_versions_plan_id", "ai_plan_versions", ["plan_id"])

    op.create_table(
        "plan_drafts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("duration", sa.String(length=20), nullable=False),
        sa.Column("focus", sa.String(length=20), nullable=True),
        sa.Column("load", sa.String(length=20), nullable=True),
        sa.Column("draft_data", postgresql.JSONB(), nullable=False),
        sa.Column("total_days", sa.Integer(), nullable=False),
        sa.Column("total_steps", sa.Integer(), nullable=False),
        sa.Column("is_valid", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.CheckConstraint(
            "total_days = ANY (ARRAY[7, 14, 21, 90])",
            name="plan_drafts_total_days_canonical_check",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="plan_drafts_user_id_fkey"),
        sa.PrimaryKeyConstraint("id", name="plan_drafts_pkey"),
    )
    op.create_index("idx_plan_drafts_user_id", "plan_drafts", ["user_id"])
    op.create_index("ix_plan_drafts_user_id", "plan_drafts", ["user_id"])

    op.create_table(
        "plan_draft_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("draft_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("day_number", sa.Integer(), nullable=False),
        sa.Column("exercise_id", sa.String(length=50), nullable=False),
        sa.Column("slot_type", sa.String(length=20), nullable=True),
        sa.Column("time_slot", sa.String(length=20), nullable=False),
        sa.Column("category", sa.String(length=30), nullable=True),
        sa.Column("difficulty", sa.Integer(), nullable=True),
        sa.Column("mechanic", sa.String(length=10), server_default="switch", nullable=True),
        sa.CheckConstraint("mechanic IN ('switch','unload')", name="plan_draft_steps_mechanic_check"),
        sa.ForeignKeyConstraint(
            ["draft_id"], ["plan_drafts.id"], name="plan_draft_steps_draft_id_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name="plan_draft_steps_pkey"),
    )
    op.create_index("idx_plan_draft_steps_draft_id", "plan_draft_steps", ["draft_id"])
    op.create_index("ix_plan_draft_steps_draft_id", "plan_draft_steps", ["draft_id"])

    op.create_table(
        "plan_instances",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("blueprint_id", sa.Text(), nullable=True),
        sa.Column(
            "initial_parameters",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=True,
        ),
        sa.Column("contract_version", sa.Text(), server_default="v1", nullable=False),
        sa.Column("schema_version", sa.Text(), server_default="v1", nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="plan_instances_user_id_fkey"),
        sa.PrimaryKeyConstraint("id", name="plan_instances_pkey"),
    )
    op.create_index("idx_plan_instances_user_id", "plan_instances", ["user_id"])

    op.create_table(
        "plan_execution_windows",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("engagement_status", sa.Text(), nullable=False),
        sa.Column(
            "start_date",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=True,
        ),
        sa.Column("end_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_load_mode", sa.Text(), server_default="LITE", nullable=True),
        sa.Column("adaptation_requests_count", sa.Integer(), server_default="0", nullable=True),
        sa.Column("batch_completion_count", sa.Integer(), server_default="0", nullable=True),
        sa.Column("hidden_compensation_score", sa.REAL(), server_default="0", nullable=True),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["plan_instances.id"],
            name="plan_execution_windows_instance_id_fkey",
        ),
        sa.PrimaryKeyConstraint("id", name="plan_execution_windows_pkey"),
    )
    op.create_index(
        "idx_plan_execution_windows_instance_id", "plan_execution_windows", ["instance_id"]
    )

    op.create_table(
        "user_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("plan_execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_id", sa.Text(), nullable=True),
        sa.Column("time_of_day_bucket", sa.Text(), nullable=False),
        sa.Column("context", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.ForeignKeyConstraint(
            ["plan_execution_id"],
            ["plan_execution_windows.id"],
            name="user_events_plan_execution_id_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["step_id"], ["content_library.id"], name="user_events_step_id_fkey"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="user_events_user_id_fkey"),
        sa.PrimaryKeyConstraint("id", name="user_events_pkey"),
    )
    op.create_index("idx_user_events_context_gin", "user_events", ["context"], postgresql_using="gin")
    op.create_index("idx_user_events_plan_execution_id", "user_events", ["plan_execution_id"])
    op.create_index("idx_user_events_step_id", "user_events", ["step_id"])
    op.create_index("idx_user_events_user_id", "user_events", ["user_id"])

    op.create_table(
        "task_stats",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("step_id", sa.Text(), nullable=False),
        sa.Column("attempts_total", sa.Integer(), server_default="0", nullable=True),
        sa.Column("completed_total", sa.Integer(), server_default="0", nullable=True),
        sa.Column("skipped_total", sa.Integer(), server_default="0", nullable=True),
        sa.Column("avg_reaction_sec", sa.REAL(), server_default="0", nullable=True),
        sa.Column("completed_edge_of_day", sa.Integer(), server_default="0", nullable=True),
        sa.Column("last_failure_reason", sa.Text(), nullable=True),
        sa.Column("history_ref", sa.Boolean(), server_default=sa.text("false"), nullable=True),
        sa.ForeignKeyConstraint(["step_id"], ["content_library.id"], name="task_stats_step_id_fkey"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="task_stats_user_id_fkey"),
        sa.PrimaryKeyConstraint("user_id", "step_id", name="task_stats_pkey"),
    )

    op.create_table(
        "failure_signals",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("plan_execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_id", sa.Text(), nullable=False),
        sa.Column("trigger_event", sa.Text(), nullable=False),
        sa.Column("failure_context_tag", sa.Text(), nullable=True),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["plan_execution_id"],
            ["plan_execution_windows.id"],
            name="failure_signals_plan_execution_id_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["step_id"], ["content_library.id"], name="failure_signals_step_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="failure_signals_user_id_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name="failure_signals_pkey"),
    )
    op.create_index(
        "idx_failure_signals_plan_execution_id", "failure_signals", ["plan_execution_id"]
    )
    op.create_index("idx_failure_signals_user_id", "failure_signals", ["user_id"])

    op.create_table(
        "user_daily_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("log_date", sa.Date(), nullable=False),
        sa.Column("mood", sa.Integer(), nullable=True),
        sa.Column("stress", sa.Integer(), nullable=True),
        sa.Column("energy", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="user_daily_logs_user_id_fkey", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="user_daily_logs_pkey"),
        sa.UniqueConstraint("user_id", "log_date", name="uniq_user_log_per_day"),
    )


def downgrade() -> None:
    raise RuntimeError(
        "The physical-schema baseline is an adoption boundary and cannot be downgraded."
    )
