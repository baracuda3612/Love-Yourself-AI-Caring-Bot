
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, Text, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy.sql import func
from app.config import DB_URL

engine = create_engine(DB_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()

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
    prompt_template = Column(Text, default="You are a concise wellbeing coach in Ukrainian. End with 1 actionable step and 1 reflective question.")
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

def init_db():
    Base.metadata.create_all(bind=engine)
