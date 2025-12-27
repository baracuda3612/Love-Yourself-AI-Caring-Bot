"""Database models and session management for the multi-agent architecture."""

from enum import Enum as PyEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    Time,
    create_engine,
)
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


# -------------------- CORE --------------------
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, unique=True, nullable=False, index=True)
    username = Column(String)
    first_name = Column(String)
    current_state = Column(String, default="onboarding:start", index=True)

    timezone = Column(String, default="Europe/Kyiv")
    notification_time = Column(Time, nullable=True)
    is_active = Column(Boolean, default=True)

    chat_history = relationship("ChatHistory", back_populates="user", cascade="all, delete-orphan")
    profile = relationship("UserProfile", back_populates="user", uselist=False, cascade="all, delete-orphan")
    facts = relationship("UserFact", back_populates="user", cascade="all, delete-orphan")
    plans = relationship("AIPlan", back_populates="user", cascade="all, delete-orphan")
    daily_logs = relationship("UserDailyLog", back_populates="user", cascade="all, delete-orphan")


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
    
    # Versioning for Adaptation
    adaptation_version = Column(Integer, default=1) 

    current_mode = Column(String, default="standard")
    milestone_status = Column(String, default="pending")
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="plans")
    days = relationship("AIPlanDay", back_populates="plan", cascade="all, delete-orphan", order_by="AIPlanDay.day_number")


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
