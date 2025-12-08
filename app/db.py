"""Database models and session management for the multi-agent architecture."""

from enum import Enum as PyEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
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
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String, nullable=False)
    description = Column(Text)
    goal = Column(Text)
    status = Column(Enum(PlanStatus), default=PlanStatus.DRAFT)
    start_date = Column(Date)
    end_date = Column(Date)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="plans")
    steps = relationship("AIPlanStep", back_populates="plan", cascade="all, delete-orphan")


class AIPlanStep(Base):
    __tablename__ = "ai_plan_steps"

    id = Column(Integer, primary_key=True)
    plan_id = Column(Integer, ForeignKey("ai_plans.id"), nullable=False)
    day_number = Column(Integer, nullable=False)
    time_slot = Column(String, nullable=True)
    content = Column(Text, nullable=False)
    content_type = Column(String)
    is_completed = Column(Boolean, default=False)
    job_id = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    plan = relationship("AIPlan", back_populates="steps")


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
