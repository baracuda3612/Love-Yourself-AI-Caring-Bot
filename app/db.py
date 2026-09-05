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
    ForeignKeyConstraint,
    Float,
    Index,
    Integer,
    JSON,
    Numeric,
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

EXPECTED_ALEMBIC_REVISION = "20260905_event_privacy"


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
    first_seen_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=True)
    first_seen_at_source = Column(String(32), server_default="created", nullable=True)
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
    feedback_events = relationship(
        "FeedbackEvent", back_populates="user", cascade="all, delete-orphan"
    )
    notice_acknowledgements = relationship(
        "NoticeAcknowledgement", back_populates="user", cascade="all, delete-orphan"
    )
    deployment_enrollments = relationship(
        "DeploymentEnrollment", back_populates="user", cascade="all, delete-orphan"
    )
    aggregate_records = relationship(
        "AggregateRecord", back_populates="user", cascade="all, delete-orphan"
    )
    report_access_grants = relationship(
        "ReportAccessGrant", back_populates="user", cascade="all, delete-orphan"
    )
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
        UniqueConstraint("id", "user_id", name="uq_ai_plans_id_user"),
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
            "(version = 0 AND "
            "step_status IN ('completed','skipped','expired','canceled') AND "
            "terminal_at IS NULL) OR "
            "(version > 0 AND "
            "(step_status IN ('completed','skipped','expired','canceled')) = "
            "(terminal_at IS NOT NULL))",
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
    __table_args__ = (
        UniqueConstraint("id", "content_version", name="uq_content_library_identity"),
    )

    id = Column(String, primary_key=True)
    content_version = Column(Integer, default=1, nullable=False)
    internal_name = Column(String, nullable=False)
    category = Column(String, nullable=False)
    difficulty = Column(Integer, nullable=False)
    energy_cost = Column(String, nullable=False)
    logic_tags = Column(JSONB, nullable=False, default=dict)
    content_payload = Column(JSONB, nullable=False, default=dict)
    is_active = Column(Boolean, default=True, nullable=False)


# -------------------- DEPLOYMENT / PRIVACY --------------------
class PrivacyNoticeVersion(Base):
    __tablename__ = "privacy_notice_versions"

    id = Column(BigInteger, primary_key=True)
    version = Column(String(64), nullable=False, unique=True)
    published_at = Column(DateTime(timezone=True), nullable=False)
    content_digest = Column(String(128), nullable=False)
    content_location = Column(String(512), nullable=False)
    is_current = Column(Boolean, nullable=False, default=False)
    retired_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Organization(Base):
    __tablename__ = "organizations"

    id = Column(BigInteger, primary_key=True)
    organization_key = Column(String(96), nullable=False, unique=True)
    display_name = Column(String(160), nullable=False)
    commercial_metadata = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Deployment(Base):
    __tablename__ = "deployments"
    __table_args__ = (
        UniqueConstraint(
            "id", "organization_id", name="uq_deployments_id_organization"
        ),
    )

    id = Column(BigInteger, primary_key=True)
    organization_id = Column(
        BigInteger, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    deployment_key = Column(String(96), nullable=False, unique=True)
    environment = Column(
        Enum("testnet", "production", name="deployment_environment"), nullable=False
    )
    starts_at = Column(DateTime(timezone=True), nullable=True)
    ends_at = Column(DateTime(timezone=True), nullable=True)
    renewal_due_at = Column(DateTime(timezone=True), nullable=True)
    enrollment_open = Column(Boolean, nullable=False, default=False)
    delivery_enabled = Column(Boolean, nullable=False, default=False)
    timezone_mode = Column(
        Enum("single", "distributed", name="deployment_timezone_mode"), nullable=False
    )
    default_timezone = Column(String(64), nullable=True)
    notice_version_id = Column(
        BigInteger,
        ForeignKey("privacy_notice_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    eligible_count_at_launch = Column(Integer, nullable=True)
    champion_contact_ref = Column(String(160), nullable=True)
    support_contact_ref = Column(String(160), nullable=True)
    roster_reconciliation_days = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AccessIdentity(Base):
    __tablename__ = "access_identities"
    __table_args__ = (
        UniqueConstraint(
            "identity_digest", "provider", name="uq_access_identities_digest_provider"
        ),
    )

    id = Column(BigInteger, primary_key=True)
    identity_digest = Column(String(128), nullable=False)
    provider = Column(String(64), nullable=False)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    verification_metadata = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class DeploymentRosterVersion(Base):
    __tablename__ = "deployment_roster_versions"
    __table_args__ = (
        UniqueConstraint(
            "deployment_id", "version", name="uq_deployment_roster_versions_version"
        ),
        UniqueConstraint(
            "id",
            "deployment_id",
            name="uq_deployment_roster_versions_id_deployment",
        ),
    )

    id = Column(BigInteger, primary_key=True)
    deployment_id = Column(
        BigInteger, ForeignKey("deployments.id", ondelete="RESTRICT"), nullable=False
    )
    version = Column(Integer, nullable=False)
    import_mode = Column(
        Enum("full_snapshot", "delta", name="roster_import_mode"), nullable=False
    )
    source_ref = Column(String(160), nullable=False)
    source_as_of = Column(DateTime(timezone=True), nullable=True)
    validated_at = Column(DateTime(timezone=True), nullable=True)
    applied_at = Column(DateTime(timezone=True), nullable=True)
    eligible_count = Column(Integer, nullable=False)
    invalid_count = Column(Integer, nullable=False, default=0)
    is_current = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class DeploymentRosterEntry(Base):
    __tablename__ = "deployment_roster_entries"
    __table_args__ = (
        UniqueConstraint(
            "roster_version_id", "entry_key", name="uq_deployment_roster_entries_key"
        ),
    )

    id = Column(BigInteger, primary_key=True)
    roster_version_id = Column(
        BigInteger,
        ForeignKey("deployment_roster_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    access_identity_id = Column(
        BigInteger,
        ForeignKey("access_identities.id", ondelete="RESTRICT"),
        nullable=False,
    )
    entry_key = Column(String(160), nullable=False)
    delta_action = Column(String(16), nullable=True)
    validation_metadata = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AccessEntitlement(Base):
    __tablename__ = "access_entitlements"
    __table_args__ = (
        UniqueConstraint("id", "deployment_id", name="uq_access_entitlements_id_deployment"),
        ForeignKeyConstraint(
            ["granting_roster_version_id", "deployment_id"],
            [
                "deployment_roster_versions.id",
                "deployment_roster_versions.deployment_id",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["confirming_roster_version_id", "deployment_id"],
            [
                "deployment_roster_versions.id",
                "deployment_roster_versions.deployment_id",
            ],
            ondelete="RESTRICT",
        ),
    )

    id = Column(BigInteger, primary_key=True)
    deployment_id = Column(
        BigInteger, ForeignKey("deployments.id", ondelete="RESTRICT"), nullable=False
    )
    access_identity_id = Column(
        BigInteger,
        ForeignKey("access_identities.id", ondelete="RESTRICT"),
        nullable=False,
    )
    granted_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    source = Column(String(64), nullable=False)
    granting_roster_version_id = Column(BigInteger, nullable=True)
    confirming_roster_version_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class DeploymentInvitation(Base):
    __tablename__ = "deployment_invitations"

    id = Column(BigInteger, primary_key=True)
    entitlement_id = Column(
        BigInteger, ForeignKey("access_entitlements.id", ondelete="RESTRICT"), nullable=False
    )
    token_digest = Column(String(128), nullable=False, unique=True)
    token_version = Column(Integer, nullable=False)
    source_operation_id = Column(String(160), nullable=False, unique=True)
    issued_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    redeemed_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)


class DeploymentEnrollment(Base):
    __tablename__ = "deployment_enrollments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["entitlement_id", "deployment_id"],
            ["access_entitlements.id", "access_entitlements.deployment_id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "id", "user_id", "deployment_id", name="uq_deployment_enrollments_identity"
        ),
    )

    id = Column(BigInteger, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    deployment_id = Column(
        BigInteger, ForeignKey("deployments.id", ondelete="RESTRICT"), nullable=False
    )
    entitlement_id = Column(BigInteger, nullable=False)
    enrolled_at = Column(DateTime(timezone=True), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    ended_reason = Column(String(64), nullable=True)
    attribution_source = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User", back_populates="deployment_enrollments")


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


class EventCatalog(Base):
    __tablename__ = "event_catalog"

    event_name = Column(String(96), primary_key=True)
    event_schema_version = Column(Integer, primary_key=True)
    event_kind = Column(
        Enum("user_behavior", "operational", "access_control", name="event_kind"),
        nullable=False,
    )
    allowed_property_schema = Column(JSONB, nullable=False)
    required_linkage = Column(JSONB, nullable=False)
    activated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    retired_at = Column(DateTime(timezone=True), nullable=True)


class UserEvent(Base):
    __tablename__ = "user_events"
    __table_args__ = (
        UniqueConstraint(
            "event_source",
            "source_operation_id",
            "event_name",
            name="uq_user_events_source_operation",
        ),
        ForeignKeyConstraint(
            ["event_name", "event_schema_version"],
            ["event_catalog.event_name", "event_catalog.event_schema_version"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["exercise_id", "content_version"],
            ["content_library.id", "content_library.content_version"],
            ondelete="RESTRICT",
            match="FULL",
        ),
        ForeignKeyConstraint(
            ["deployment_id", "organization_id"],
            ["deployments.id", "deployments.organization_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["deployment_enrollment_id", "user_id", "deployment_id"],
            [
                "deployment_enrollments.id",
                "deployment_enrollments.user_id",
                "deployment_enrollments.deployment_id",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["plan_id", "user_id"],
            ["ai_plans.id", "ai_plans.user_id"],
            ondelete="RESTRICT",
        ),
        Index("ix_user_events_user_time", "user_id", text("occurred_at DESC"), "event_id"),
        Index("ix_user_events_plan_time", "plan_id", "occurred_at", "event_id"),
        Index("ix_user_events_step_time", "plan_step_id", "occurred_at", "event_id"),
        Index(
            "ix_user_events_deployment_time",
            "deployment_id",
            "occurred_at",
            "event_id",
        ),
    )

    event_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_name = Column(String(96), nullable=True)
    event_schema_version = Column(Integer, nullable=True)
    occurred_at = Column(DateTime(timezone=True), nullable=True)
    recorded_at = Column(DateTime(timezone=True), nullable=True)
    event_source = Column(String(64), nullable=True)
    source_operation_id = Column(String(160), nullable=True)
    environment = Column(
        Enum("testnet", "production", name="deployment_environment"), nullable=True
    )
    organization_id = Column(
        BigInteger, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True
    )
    deployment_id = Column(BigInteger, nullable=True)
    deployment_enrollment_id = Column(BigInteger, nullable=True)
    plan_id = Column(Integer, nullable=True)
    plan_step_id = Column(
        Integer, ForeignKey("ai_plan_steps.id", ondelete="RESTRICT"), nullable=True
    )
    exercise_id = Column(Text, nullable=True)
    content_version = Column(Integer, nullable=True)
    timezone_basis = Column(String(64), nullable=True)
    time_of_day_bucket = Column(String, nullable=False)
    properties = Column(JSONB, nullable=True)

    # Inert WP-01.4 compatibility storage.  Canonical writers and readers do
    # not use these columns; WP-08.1 removes them after deployed-use evidence.
    event_type = Column(String, nullable=True)
    timestamp = Column(DateTime(timezone=True), nullable=True)
    plan_execution_id = Column(
        UUID(as_uuid=True),
        ForeignKey("plan_execution_windows.id"),
        nullable=True,
        index=True,
    )
    step_id = Column(Text, ForeignKey("content_library.id"), nullable=True)
    context = Column(JSONB, nullable=True)

    user = relationship("User", back_populates="events")
    plan_execution_window = relationship("PlanExecutionWindow", back_populates="events")


class FeedbackEvent(Base):
    __tablename__ = "feedback_events"
    __table_args__ = (
        UniqueConstraint("source", "source_operation_id", name="uq_feedback_events_source_operation"),
        Index("ix_feedback_user_time", "user_id", text("created_at DESC")),
    )

    id = Column(BigInteger, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    source = Column(
        Enum(
            "exercise_efficacy",
            "coach_quality",
            "product_feedback",
            name="feedback_source",
        ),
        nullable=False,
    )
    source_operation_id = Column(String(160), nullable=False)
    plan_step_id = Column(
        Integer, ForeignKey("ai_plan_steps.id", ondelete="RESTRICT"), nullable=True
    )
    coach_message_id = Column(
        BigInteger, ForeignKey("chat_history.id", ondelete="RESTRICT"), nullable=True
    )
    source_message_id = Column(
        BigInteger, ForeignKey("chat_history.id", ondelete="RESTRICT"), nullable=True
    )
    value = Column(String(64), nullable=False)
    reason = Column(String(160), nullable=True)
    category = Column(
        Enum(
            "bug",
            "confusion",
            "feature_request",
            "content",
            "coach",
            "other",
            name="feedback_category",
        ),
        nullable=True,
    )
    extracted_text = Column(Text, nullable=True)
    context = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User", back_populates="feedback_events")


class NoticeAcknowledgement(Base):
    __tablename__ = "notice_acknowledgements"
    __table_args__ = (
        UniqueConstraint("source_operation_id", name="uq_notice_ack_source_operation"),
        UniqueConstraint(
            "user_id", "deployment_id", "notice_version_id", name="uq_notice_ack_identity"
        ),
    )

    id = Column(BigInteger, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    deployment_id = Column(
        BigInteger, ForeignKey("deployments.id", ondelete="RESTRICT"), nullable=False
    )
    notice_version_id = Column(
        BigInteger,
        ForeignKey("privacy_notice_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    acknowledged_at = Column(DateTime(timezone=True), nullable=False)
    source_operation_id = Column(String(160), nullable=False)

    user = relationship("User", back_populates="notice_acknowledgements")


class ReportAccessGrant(Base):
    __tablename__ = "report_access_grants"
    __table_args__ = (
        ForeignKeyConstraint(
            ["plan_id", "user_id"],
            ["ai_plans.id", "ai_plans.user_id"],
            ondelete="CASCADE",
        ),
    )

    id = Column(BigInteger, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    plan_id = Column(Integer, nullable=False)
    purpose = Column(
        Enum("completion_report", "pulse_report", name="report_grant_purpose"),
        nullable=False,
    )
    token_digest = Column(String(128), nullable=False, unique=True)
    token_version = Column(Integer, nullable=False)
    issued_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revocation_reason = Column(String(160), nullable=True)

    user = relationship("User", back_populates="report_access_grants")


class AggregateRecord(Base):
    __tablename__ = "aggregate_records"
    __table_args__ = (
        Index(
            "uq_aggregate_contribution_source",
            "source_operation_id",
            unique=True,
            postgresql_where=text("record_kind = 'contribution'"),
        ),
        Index(
            "uq_aggregate_sealed_cell_revision",
            "metric_name",
            "metric_schema_version",
            "period_start",
            "period_end",
            "dimension_key",
            "revision",
            unique=True,
            postgresql_where=text("record_kind = 'sealed_cell'"),
        ),
        Index(
            "uq_aggregate_sealed_supersedes",
            "supersedes_record_id",
            unique=True,
            postgresql_where=text("supersedes_record_id IS NOT NULL"),
        ),
        Index(
            "ix_aggregate_records_cell",
            "metric_name",
            "metric_schema_version",
            "period_start",
            "period_end",
            "dimension_key",
        ),
    )

    id = Column(BigInteger, primary_key=True)
    record_kind = Column(
        Enum("contribution", "sealed_cell", name="aggregate_record_kind"), nullable=False
    )
    metric_name = Column(String(96), nullable=False)
    metric_schema_version = Column(Integer, nullable=False)
    period_start = Column(DateTime(timezone=True), nullable=False)
    period_end = Column(DateTime(timezone=True), nullable=False)
    dimension_key = Column(String(64), nullable=False)
    dimensions = Column(JSONB, nullable=False)
    numeric_value = Column(Numeric(18, 6), nullable=False)
    sample_count = Column(Integer, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    source_operation_id = Column(String(160), nullable=True)
    retention_until = Column(DateTime(timezone=True), nullable=True)
    sealed_at = Column(DateTime(timezone=True), nullable=True)
    gate_eligible_count = Column(Integer, nullable=True)
    gate_contributor_count = Column(Integer, nullable=True)
    revision = Column(Integer, nullable=False, default=1)
    supersedes_record_id = Column(
        BigInteger, ForeignKey("aggregate_records.id", ondelete="RESTRICT"), nullable=True
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User", back_populates="aggregate_records")


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
