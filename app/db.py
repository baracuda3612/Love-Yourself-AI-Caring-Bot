"""Database models and session management for the multi-agent architecture."""

from enum import Enum as PyEnum
import logging
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Float,
    Index,
    Integer,
    JSON,
    String,
    Text,
    Time,
    UniqueConstraint,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from sqlalchemy.sql import func

from app.config import settings
from app.schemas.planner import PlanModule, StepType, DifficultyLevel

engine = create_engine(settings.DATABASE_URL, echo=False, future=True)
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    expire_on_commit=False,
)
Base = declarative_base()
logger = logging.getLogger(__name__)

EXPECTED_ALEMBIC_REVISION = "20260902_plan_lifecycle"


class SchemaVersionError(RuntimeError):
    """The database is not at the application schema revision required by this build."""


def audit_startup_schema() -> None:
    """Fail closed unless the read-only Alembic version check matches this build."""
    inspector = inspect(engine)
    if not inspector.has_table("alembic_version"):
        logger.critical("Startup schema audit failed: Alembic version table is missing.")
        raise SchemaVersionError("Startup schema audit failed: database is not under Alembic authority")

    with engine.connect() as connection:
        revisions = tuple(
            connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
        )

    if revisions != (EXPECTED_ALEMBIC_REVISION,):
        logger.critical("Startup schema audit failed: database revision is incompatible.")
        raise SchemaVersionError("Startup schema audit failed: database revision is incompatible")


class PlanStatus(PyEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class FactCategory(PyEnum):
    GOAL = "goal"
    PREFERENCE = "preference"
    MEDICAL = "medical"
    BIOGRAPHY = "biography"
    INSIGHT = "insight"


class EngagementStatus(PyEnum):
    ACTIVE = "ACTIVE"
    SPORADIC = "SPORADIC"
    RETURNING = "RETURNING"
    DORMANT = "DORMANT"


# -------------------- CORE --------------------
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, unique=True, nullable=False, index=True)
    username = Column(String)
    first_name = Column(String)
    # WP-01.3 compatibility storage only. Runtime lifecycle never reads or
    # writes these columns; WP-02.1/WP-08.1 own their eventual removal.
    current_state = Column(String, nullable=True)
    last_active_at = Column(DateTime(timezone=True), nullable=True)
    plan_end_date = Column(DateTime(timezone=True), nullable=True)

    timezone = Column(String, default="Europe/Kyiv")
    notification_time = Column(Time, nullable=True)
    is_active = Column(Boolean, default=True)

    chat_history = relationship("ChatHistory", back_populates="user", cascade="all, delete-orphan")
    profile = relationship("UserProfile", back_populates="user", uselist=False, cascade="all, delete-orphan")
    facts = relationship("UserFact", back_populates="user", cascade="all, delete-orphan")
    plans = relationship("AIPlan", back_populates="user", cascade="all, delete-orphan")
    daily_logs = relationship("UserDailyLog", back_populates="user", cascade="all, delete-orphan")
    plan_instances = relationship("PlanInstance", back_populates="user", cascade="all, delete-orphan")
    events = relationship("UserEvent", back_populates="user", cascade="all, delete-orphan")
    onboarding_progress = relationship(
        "OnboardingProgress",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )


class OnboardingProgress(Base):
    """Setup progress before a plan exists; never a plan-lifecycle mirror."""

    __tablename__ = "onboarding_progress"
    __table_args__ = (
        CheckConstraint("length(btrim(stage)) > 0", name="ck_onboarding_progress_stage"),
    )

    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    stage = Column(String(64), nullable=False, default="START")
    started_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user = relationship("User", back_populates="onboarding_progress")


# -------------------- MEMORY --------------------
class ChatHistory(Base):
    __tablename__ = "chat_history"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    role = Column(Text, nullable=False)
    text = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("role in ('user','assistant')", name="ck_chat_history_role"),
    )

    user = relationship("User", back_populates="chat_history")


class UserProfile(Base):
    __tablename__ = "user_profiles"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False, index=True)
    main_goal = Column(Text)
    communication_style = Column(Text)
    name_preference = Column(Text)
    attributes = Column(JSON, default=dict)
    daily_time_slots = Column(
        JSONB,
        default=lambda: {
            "MORNING": "09:30",
            "DAY": "14:00",
            "EVENING": "21:00",
        },
    )
    active_days = Column(
        JSONB,
        default=lambda: ["MON", "TUE", "WED", "THU", "FRI"],
    )
    coach_persona = Column(String(20), nullable=True)
    pulse_sent_indices = Column(JSONB, nullable=True, default=list)

    # WP-01.3 compatibility storage only; never read/written as lifecycle.
    is_paused = Column(Boolean, nullable=True)
    # Legacy counter; canonical event-derived telemetry arrives in WP-01.4/07.1.
    pause_count = Column(Integer, nullable=False, default=0)
    # evening_slot_collected: True once EVENING HH:MM is stored for MEDIUM.
    # Collected exactly once on first MEDIUM plan. Never asked again.
    evening_slot_collected = Column(Boolean, nullable=False, default=False)

    user = relationship("User", back_populates="profile")


class UserFact(Base):
    __tablename__ = "user_facts"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    category = Column(Enum(FactCategory), nullable=False)
    content = Column(Text, nullable=False)
    relevance = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="facts")


# -------------------- PLANS --------------------
class AIPlan(Base):
    __tablename__ = "ai_plans"

    __table_args__ = (
        UniqueConstraint("user_id", "cycle_number", name="uq_ai_plans_user_cycle"),
        CheckConstraint("cycle_number > 0", name="ck_ai_plans_cycle_number"),
        CheckConstraint("total_days IN (7, 14)", name="ck_ai_plans_total_days"),
        CheckConstraint("version > 0", name="ck_ai_plans_version"),
        CheckConstraint(
            "activated_at IS NOT NULL AND "
            "(status <> 'abandoned' OR abandoned_at IS NOT NULL) AND "
            "(abandoned_at IS NULL OR abandoned_at >= activated_at)",
            name="ck_ai_plans_chronology",
        ),
        Index(
            "ux_ai_plans_one_current_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("status IN ('active','paused')"),
        ),
        Index("ix_ai_plans_user_created", "user_id", text("created_at DESC")),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    
    # Metadata
    title = Column(String, nullable=False)
    module_id = Column(
        Enum(
            PlanModule,
            values_callable=lambda enum: [entry.value for entry in enum],
            native_enum=True,
            name="plan_module",
        ),
        nullable=False,
        default=PlanModule.BURNOUT_RECOVERY.value,
    )
    goal_description = Column(Text) 
    
    # Status & Lifecycle
    status = Column(
        Enum("active", "paused", "completed", "abandoned", name="plan_status"),
        nullable=False,
        default="active",
    )
    cycle_number = Column(Integer, nullable=False)
    activated_at = Column(DateTime(timezone=True), nullable=False)
    abandoned_at = Column(DateTime(timezone=True), nullable=True)
    version = Column(Integer, nullable=False, default=1)
    start_date = Column(DateTime(timezone=True), server_default=func.now())
    end_date = Column(DateTime(timezone=True), nullable=True)
    # WP-01.3 compatibility storage only; progress is derived from child steps.
    current_day = Column(Integer, nullable=True)

    duration = Column(String(20), nullable=True)
    focus = Column(String(20), nullable=True)
    load = Column(String(20), nullable=True)   # nullable since T5.2 — v5 plans have no load concept
    preferred_time_slots = Column(JSONB, default=list, nullable=False)
    total_days = Column(Integer, nullable=False)
    
    current_mode = Column(String, nullable=True)
    milestone_status = Column(String, default="pending")
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="plans")
    days = relationship("AIPlanDay", back_populates="plan", cascade="all, delete-orphan", order_by="AIPlanDay.day_number")
    versions = relationship(
        "AIPlanVersion",
        back_populates="plan",
        cascade="all, delete-orphan",
        order_by="AIPlanVersion.created_at",
    )


class AIPlanDay(Base):
    __tablename__ = "ai_plan_days"

    __table_args__ = (
        UniqueConstraint("plan_id", "day_number", name="uq_ai_plan_days_plan_day"),
        CheckConstraint("day_number > 0", name="ck_ai_plan_days_day_number"),
    )
    
    id = Column(Integer, primary_key=True)
    plan_id = Column(Integer, ForeignKey("ai_plans.id"), nullable=False, index=True)
    
    day_number = Column(Integer, nullable=False) # 1, 2, 3...
    focus_theme = Column(String, nullable=True)
    
    # WP-01.3 compatibility storage only; derived from child step_status.
    is_completed = Column(Boolean, nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    
    plan = relationship("AIPlan", back_populates="days")
    steps = relationship("AIPlanStep", back_populates="day", cascade="all, delete-orphan", order_by="AIPlanStep.order_in_day")


class AIPlanStep(Base):
    __tablename__ = "ai_plan_steps"

    __table_args__ = (
        UniqueConstraint("day_id", "order_in_day", name="uq_ai_plan_steps_day_order"),
        CheckConstraint("version >= 0", name="ck_ai_plan_steps_version"),
        CheckConstraint(
            "(scheduled_for IS NULL OR expires_at IS NULL OR expires_at > scheduled_for)",
            name="ck_ai_plan_steps_schedule_chronology",
        ),
        CheckConstraint(
            "version = 0 OR "
            "(step_status IN ('completed','skipped','expired','canceled')) = "
            "(terminal_at IS NOT NULL)",
            name="ck_ai_plan_steps_terminal_timestamp",
        ),
    )

    id = Column(Integer, primary_key=True)
    day_id = Column(Integer, ForeignKey("ai_plan_days.id"), nullable=False, index=True)
    
    # Content
    exercise_id = Column(String, ForeignKey("content_library.id"), nullable=True, index=True)
    title = Column(String, nullable=False)
    description = Column(Text)
    step_type = Column(
        Enum(
            StepType,
            values_callable=lambda enum: [entry.value for entry in enum],
            native_enum=True,
            name="step_type",
        ),
        nullable=False,
        default=StepType.ACTION.value,
    )
    difficulty = Column(
        Enum(
            DifficultyLevel,
            values_callable=lambda enum: [entry.value for entry in enum],
            native_enum=True,
            name="difficulty_level",
        ),
        nullable=False,
        default=DifficultyLevel.EASY.value,
    )
    
    # Scheduling
    order_in_day = Column(Integer, default=0)
    time_slot = Column(String, default="DAY")
    # mechanic: snapshot of exercise.mechanic at plan generation time.
    # Values: 'switch' | 'unload'. Never recomputed at delivery (invariant 6, T5.1).
    mechanic = Column(String(10), nullable=False, default="switch")
    # Concrete timestamp for the scheduler (calculated from user daily_time_slots)
    scheduled_for = Column(DateTime(timezone=True), nullable=True)
    
    # Execution State
    # step_status is the canonical lifecycle field.
    # Values: pending | delivered | completed | skipped | expired
    # Legacy booleans/timestamp remain nullable and inert until WP-08.1.
    step_status = Column(
        Enum(
            "pending",
            "delivered",
            "completed",
            "skipped",
            "expired",
            "canceled",
            name="plan_step_status",
        ),
        nullable=False,
        default="pending",
    )
    terminal_at = Column(DateTime(timezone=True), nullable=True)
    version = Column(Integer, nullable=False, default=1)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    tg_message_id = Column(Integer, nullable=True)  # Telegram message_id for button removal on expiry
    is_completed = Column(Boolean, nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    skipped = Column(Boolean, nullable=True)
    slot_type = Column(String(20), default="CORE", nullable=False)
    
    day = relationship("AIPlanDay", back_populates="steps")


class PlanLifecycleOperation(Base):
    """Immutable idempotency receipt; plan/step status remains lifecycle authority."""

    __tablename__ = "plan_lifecycle_operations"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "source_operation_id", name="uq_plan_lifecycle_operation_source"
        ),
        CheckConstraint("length(btrim(operation)) > 0", name="ck_plan_lifecycle_operation_name"),
        CheckConstraint("length(btrim(result_status)) > 0", name="ck_plan_lifecycle_result"),
    )

    id = Column(BigInteger, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    plan_id = Column(Integer, ForeignKey("ai_plans.id", ondelete="CASCADE"), nullable=False)
    plan_step_id = Column(
        Integer, ForeignKey("ai_plan_steps.id", ondelete="CASCADE"), nullable=True
    )
    source_operation_id = Column(String(160), nullable=False)
    operation = Column(String(40), nullable=False)
    result_status = Column(String(24), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AIPlanVersion(Base):
    __tablename__ = "ai_plan_versions"

    id = Column(Integer, primary_key=True)
    plan_id = Column(Integer, ForeignKey("ai_plans.id"), nullable=False, index=True)
    applied_adaptation_type = Column(String, nullable=False)
    diff = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    plan = relationship("AIPlan", back_populates="versions")


class PlanDraftRecord(Base):
    __tablename__ = "plan_drafts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    status = Column(String(20), nullable=False, default="DRAFT")

    duration = Column(String(20), nullable=False)
    # focus / load: nullable since T5.2 — v5 plans do not use these concepts
    focus = Column(String(20), nullable=True)
    load = Column(String(20), nullable=True)

    draft_data = Column(JSONB, nullable=False)

    total_days = Column(Integer, nullable=False)
    total_steps = Column(Integer, nullable=False)
    is_valid = Column(Boolean, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    steps = relationship(
        "PlanDraftStep",
        back_populates="draft",
        cascade="all, delete-orphan",
        order_by="PlanDraftStep.day_number",
    )


class PlanDraftStep(Base):
    __tablename__ = "plan_draft_steps"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    draft_id = Column(UUID(as_uuid=True), ForeignKey("plan_drafts.id"), nullable=False, index=True)

    day_number = Column(Integer, nullable=False)
    exercise_id = Column(String(50), nullable=False)
    # mechanic: snapshot from library at build time (T5.2). "switch" | "unload"
    mechanic = Column(String(10), nullable=True)  # nullable: legacy rows pre-T5.2
    slot_type = Column(String(20), nullable=True)   # legacy; not used in v5
    time_slot = Column(String(20), nullable=False)
    category = Column(String(30), nullable=True)    # legacy; not used in v5
    difficulty = Column(Integer, nullable=True)     # legacy; not used in v5

    draft = relationship("PlanDraftRecord", back_populates="steps")


# -------------------- CONTENT LIBRARY --------------------
class ContentLibrary(Base):
    __tablename__ = "content_library"

    id = Column(String, primary_key=True)
    content_version = Column(Integer, default=1, nullable=False)
    internal_name = Column(String, nullable=False)
    category = Column(String, nullable=False)
    difficulty = Column(Integer, nullable=False)
    energy_cost = Column(String, nullable=False)
    logic_tags = Column(JSONB, nullable=False, default=dict)
    content_payload = Column(JSONB, nullable=False, default=dict)
    is_active = Column(Boolean, default=True, nullable=False)


# -------------------- TELEMETRY --------------------
class PlanInstance(Base):
    __tablename__ = "plan_instances"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    blueprint_id = Column(String)
    initial_parameters = Column(JSONB, nullable=False, default=dict)
    contract_version = Column(String, nullable=False, default="v1")
    schema_version = Column(String, nullable=False, default="v1")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="plan_instances")
    execution_windows = relationship(
        "PlanExecutionWindow",
        back_populates="instance",
        cascade="all, delete-orphan",
    )


class PlanExecutionWindow(Base):
    __tablename__ = "plan_execution_windows"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    instance_id = Column(UUID(as_uuid=True), ForeignKey("plan_instances.id"), nullable=False, index=True)
    engagement_status = Column(Enum(EngagementStatus), nullable=False, default=EngagementStatus.ACTIVE)
    start_date = Column(DateTime(timezone=True), server_default=func.now())
    end_date = Column(DateTime(timezone=True), nullable=True)
    current_load_mode = Column(String, default="LITE")
    adaptation_requests_count = Column(Integer, default=0)
    batch_completion_count = Column(Integer, default=0)
    hidden_compensation_score = Column(Float, default=0.0)

    instance = relationship("PlanInstance", back_populates="execution_windows")
    events = relationship("UserEvent", back_populates="plan_execution_window")


class UserEvent(Base):
    __tablename__ = "user_events"
    __table_args__ = (Index("idx_user_events_context_gin", "context", postgresql_using="gin"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    event_type = Column(String, nullable=False)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    plan_execution_id = Column(
        UUID(as_uuid=True),
        ForeignKey("plan_execution_windows.id"),
        nullable=False,
        index=True,
    )
    # TECH-DEBT TD-2:
    # step_id is Text for historical reasons and may contain:
    # - numeric plan_step_id (new system)
    # - UUID/content_id (legacy deliveries)
    # Metrics must treat this column carefully.
    # Future refactor: introduce plan_step_int (Integer, nullable) and backfill.
    step_id = Column(Text, ForeignKey("content_library.id"), nullable=True)
    time_of_day_bucket = Column(String, nullable=False)
    context = Column(JSONB, nullable=False, default=dict)

    user = relationship("User", back_populates="events")
    plan_execution_window = relationship("PlanExecutionWindow", back_populates="events")


class TaskStats(Base):
    __tablename__ = "task_stats"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    step_id = Column(Text, ForeignKey("content_library.id"), primary_key=True)
    attempts_total = Column(Integer, default=0)
    completed_total = Column(Integer, default=0)
    skipped_total = Column(Integer, default=0)
    avg_reaction_sec = Column(Float, default=0.0)
    completed_edge_of_day = Column(Integer, default=0)
    last_failure_reason = Column(String)
    history_ref = Column(Boolean, default=False)


class FailureSignal(Base):
    __tablename__ = "failure_signals"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    plan_execution_id = Column(
        UUID(as_uuid=True),
        ForeignKey("plan_execution_windows.id"),
        nullable=False,
        index=True,
    )
    step_id = Column(Text, ForeignKey("content_library.id"), nullable=False)
    trigger_event = Column(String, nullable=False)
    failure_context_tag = Column(String)
    detected_at = Column(DateTime(timezone=True), server_default=func.now())


# -------------------- ANALYTICS --------------------
class UserDailyLog(Base):
    __tablename__ = "user_daily_logs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    date = Column(String, nullable=False)
    stress_level = Column(Integer)
    energy_level = Column(Integer)
    mood_note = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="daily_logs")


# -------------------- SESSION HELPERS --------------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
