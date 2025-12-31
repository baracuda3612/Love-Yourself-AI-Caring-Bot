# app/telegram.py
# Спрощена версія для роботи з новою БД та агентною архітектурою

import logging
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

from app.config import settings
from app.db import ChatHistory, SessionLocal, User, UserProfile
from app.orchestrator import handle_incoming_message
from app.redis_client import create_fsm_storage, create_redis_client

bot = Bot(token=settings.BOT_TOKEN, parse_mode="HTML")
redis_client = create_redis_client()
storage = create_fsm_storage(redis_client) or MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)
logger = logging.getLogger(__name__)


def _ensure_user(db, tg_user) -> tuple[User, bool]:
    user: Optional[User] = db.query(User).filter(User.tg_id == tg_user.id).first()
    is_created = False
    if not user:
        user = User(
            tg_id=tg_user.id,
            username=tg_user.username,
            first_name=tg_user.first_name,
            current_state="ONBOARDING:START",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        is_created = True
    else:
        user.username = tg_user.username
        user.first_name = tg_user.first_name
    if not user.profile:
        profile = UserProfile(user_id=user.id)
        db.add(profile)
    db.commit()
    db.refresh(user)
    return user, is_created


@router.message(Command("start"))
async def cmd_start(message: Message):
    with SessionLocal() as db:
        user, is_created = _ensure_user(db, message.from_user)
        if is_created or user.current_state == "IDLE_NEW":
            if user.current_state == "IDLE_NEW":
                user.current_state = "ONBOARDING:START"
                db.commit()
            await message.answer("Привіт! Я LoveYourself бот. Давай познайомимось.")
        else:
            await message.answer("З поверненням! Продовжуємо.")
    logger.info(
        "User %s started. Created: %s, State: %s",
        user.id,
        is_created,
        user.current_state,
    )


@router.message(F.text)
async def on_text(message: Message):
    text = message.text or ""
    with SessionLocal() as db:
        user, _ = _ensure_user(db, message.from_user)
        db.add(ChatHistory(user_id=user.id, role="user", text=text))
        db.commit()

    reply_text = await handle_incoming_message(user.id, text)
    await message.answer(reply_text)

    with SessionLocal() as db:
        db.add(ChatHistory(user_id=user.id, role="assistant", text=reply_text))
        db.commit()


__all__ = ["bot", "dp", "router"]
