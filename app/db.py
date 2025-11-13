# app/db.py
# Моделі під чернетки планів: proposed_for (у draft), scheduled_for (після approve),
# статуси кроків, approved_at у плану. Без магії, без рантайм-міграцій.

from sqlalchemy import (
    create_engine, Column, Integer, String, Boolean, DateTime, Text, ForeignKey, JSON
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy.sql import func
from app.config import settings
import uuid

engine = create_engine(settings.DATABASE_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()

# -------------------- БАЗОВІ ТАБЛИЦІ --------------------

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    tg_id = Column(Integer, index=True, unique=True, nullable=False)
    first_name = Column(String)
    username = Column(String)
    timezone = Column(String, default="Europe/Kyiv")
    send_hour = Column(Integer, default=9)
    daily_limit = Column(Integer, default=10)
    active = Column(Boolean, default=True)
    prompt_template = Column(
        Text,
        default="You are a concise wellbeing coach in Ukrainian. End with 1 actionable step and 1 reflective question."
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Delivery(Base):
    __tablename__ = "deliveries"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    scheduled_for = Column(DateTime(timezone=True), nullable=False)
    sent_at = Column(DateTime(timezone=True))
    status = Column(String, default="scheduled")
    message_id = Column(Integer)
    prompt_snapshot = Column(Text)
    model = Column(String)
    tokens_prompt = Column(Integer, default=0)
    tokens_completion = Column(Integer, default=0)
    tokens_total = Column(Integer, default=0)


class Response(Base):
    __tablename__ = "responses"

    id = Column(Integer, primary_key=True)
    delivery_id = Column(Integer, nullable=True)
    user_id = Column(Integer)
    kind = Column(String)
    payload = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class UsageCounter(Base):
    __tablename__ = "usage_counters"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer)
    day = Column(String, index=True)
    ask_count = Column(Integer, default=0)
    month = Column(String, index=True)
    month_ask_count = Column(Integer, default=0)

# -------------------- НОВІ ТАБЛИЦІ --------------------

class UserMemoryProfile(Base):
    __tablename__ = "user_memory_profiles"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    profile_data = Column(JSON, nullable=False)  # структурований JSON профілю пам'яті
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class UserReminder(Base):
    __tablename__ = "user_reminders"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    job_id = Column(String, unique=True, nullable=False)   # APScheduler job_id
    message = Column(Text, nullable=False)
    cron_expression = Column(String, nullable=True)        # якщо повторюване (cron-like)
    scheduled_at = Column(DateTime(timezone=True), nullable=True)  # якщо одноразове
    timezone = Column(String, nullable=False)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    @staticmethod
    def generate_job_id(user_id: int, reminder_type: str = "reminder") -> str:
        return f"user_{user_id}_{reminder_type}_{uuid.uuid4().hex[:8]}"

class AIPlan(Base):
    __tablename__ = "ai_plans"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String, nullable=False)
    description = Column(Text)
    goal = Column(Text)
    duration_days = Column(Integer)
    send_hour = Column(Integer)
    send_minute = Column(Integer)
    tasks_per_day = Column(Integer)

    # Життєвий цикл: draft -> active -> (completed|canceled)
    status = Column(String, default="draft", server_default="draft")
    approved_at = Column(DateTime(timezone=True))

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True))

    steps = relationship("AIPlanStep", back_populates="plan", cascade="all, delete-orphan")

class AIPlanStep(Base):
    __tablename__ = "ai_plan_steps"

    id = Column(Integer, primary_key=True)
    plan_id = Column(Integer, ForeignKey("ai_plans.id"), nullable=False)
    job_id = Column(String, unique=True, nullable=True)  # APScheduler job id
    status = Column(String, default="pending")  # pending/approved/canceled
    message = Column(Text, nullable=False)

    # У чернетці зберігаємо proposed_for (UTC). Після approve — виставляємо scheduled_for + job_id.
    proposed_for = Column(DateTime(timezone=True))                    # може бути NULL у старих
    scheduled_for = Column(DateTime(timezone=True), nullable=True)    # NULL у draft
    status = Column(String, default="pending", server_default="pending")  # pending/approved/completed/canceled

    is_completed = Column(Boolean, default=False)
    completed_at = Column(DateTime(timezone=True))

    plan = relationship("AIPlan", back_populates="steps")

    @staticmethod
    def generate_job_id(user_id: int, plan_id: int) -> str:
        return f"user_{user_id}_plan_{plan_id}_step_{uuid.uuid4().hex[:8]}"

# -------------------- ІНІТ --------------------

def init_db():
    # Якщо ти без Alembic — це створить відсутні таблиці (але не змінить існуючі схеми)
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
