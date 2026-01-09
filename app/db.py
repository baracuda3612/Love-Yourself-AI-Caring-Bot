"""Database models and session management for the multi-agent architecture."""

from enum import Enum as PyEnum
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
    create_engine,
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


class PlanStatus(PyEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    COMPLETED = "completed"
    PAUSED = "paused"
    CANCELED = "canceled"


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

    __table_args__ = (
        CheckConstraint(
            "current_state IN ("
            "'IDLE_NEW','IDLE_ONBOARDED','IDLE_PLAN_ABORTED','IDLE_FINISHED','IDLE_DROPPED',"
            "'PLAN_FLOW:DATA_COLLECTION','PLAN_FLOW:CONFIRMATION_PENDING',"
            "'PLAN_FLOW:FINALIZATION','ACTIVE','ACTIVE_PAUSED','ADAPTATION_FLOW'"
            ") OR current_state LIKE 'ONBOARDING:%'",
            name="ck_users_current_state",
        ),
        CheckConstraint(
            "execution_policy IN ('EXECUTION','OBSERVATION')",
            name="ck_users_execution_policy",
        ),
        CheckConstraint(
            "current_load IN ('LITE','MID','INTENSIVE')",
            name="ck_users_current_load",
        ),
    )

    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, unique=True, nullable=False, index=True)
    username = Column(String)
    first_name = Column(String)
    current_state = Column(String, default="IDLE_NEW", index=True)
    execution_policy = Column(String, default="EXECUTION", nullable=False)
    current_load = Column(String, default="LITE", nullable=False)
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

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    
    # Metadata
    title = Column(String, nullable=False)
    module_id = Column(Enum(PlanModule), nullable=False, default=PlanModule.BURNOUT_RECOVERY)
    goal_description = Column(Text) 
    
    # Status & Lifecycle
    status = Column(Enum("active", "completed", "paused", "abandoned", name="plan_status_enum"), default="active")
    start_date = Column(DateTime(timezone=True), server_default=func.now())
    end_date = Column(DateTime(timezone=True), nullable=True)
    execution_policy = Column(String, default="active", nullable=False)
    
    # Versioning for Adaptation
    adaptation_version = Column(Integer, default=1) 

    current_mode = Column(String, default="standard")
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
    
    id = Column(Integer, primary_key=True)
    plan_id = Column(Integer, ForeignKey("ai_plans.id"), nullable=False, index=True)
    
    day_number = Column(Integer, nullable=False) # 1, 2, 3...
    focus_theme = Column(String, nullable=True)
    
    is_completed = Column(Boolean, default=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    
    plan = relationship("AIPlan", back_populates="days")
    steps = relationship("AIPlanStep", back_populates="day", cascade="all, delete-orphan", order_by="AIPlanStep.order_in_day")


class AIPlanStep(Base):
    __tablename__ = "ai_plan_steps"

    id = Column(Integer, primary_key=True)
    day_id = Column(Integer, ForeignKey("ai_plan_days.id"), nullable=False, index=True)
    
    # Content
    title = Column(String, nullable=False)
    description = Column(Text)
    step_type = Column(Enum(StepType), default=StepType.ACTION)
    difficulty = Column(Enum(DifficultyLevel), default=DifficultyLevel.EASY)
    
    # Scheduling
    order_in_day = Column(Integer, default=0)
    time_of_day = Column(String, default="any")
    # Concrete timestamp for the scheduler (calculated by Manager Agent based on time_of_day)
    scheduled_for = Column(DateTime(timezone=True), nullable=True)
    
    # Execution State
    is_completed = Column(Boolean, default=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    skipped = Column(Boolean, default=False)
    
    day = relationship("AIPlanDay", back_populates="steps")


class AIPlanVersion(Base):
    __tablename__ = "ai_plan_versions"

    id = Column(Integer, primary_key=True)
    plan_id = Column(Integer, ForeignKey("ai_plans.id"), nullable=False, index=True)
    applied_adaptation_type = Column(String, nullable=False)
    diff = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    plan = relationship("AIPlan", back_populates="versions")


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
def init_db():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
