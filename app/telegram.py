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


def _ensure_user(db, tg_user) -> User:
    user: Optional[User] = db.query(User).filter(User.tg_id == tg_user.id).first()
    if not user:
        user = User(
            tg_id=tg_user.id,
            username=tg_user.username,
            first_name=tg_user.first_name,
            current_state="onboarding:start",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        user.username = tg_user.username
        user.first_name = tg_user.first_name
    if not user.profile:
        profile = UserProfile(user_id=user.id)
        db.add(profile)
    if user.current_state != "idle":
        user.current_state = "idle"
    db.commit()
    db.refresh(user)
    return user


@router.message(Command("start"))
async def cmd_start(message: Message):
    with SessionLocal() as db:
        user = _ensure_user(db, message.from_user)
    await message.answer(
        "Привіт! Я LoveYourself бот. Напиши повідомлення, і я відповім у новому форматі."
    )
    logger.info("User %s started the bot", user.id)


@router.message(F.text)
async def on_text(message: Message):
    text = message.text or ""
    with SessionLocal() as db:
        user = _ensure_user(db, message.from_user)
        db.add(ChatHistory(user_id=user.id, role="user", text=text))
        db.commit()

    reply_text = await handle_incoming_message(user.id, text)
    await message.answer(reply_text)

    with SessionLocal() as db:
        db.add(ChatHistory(user_id=user.id, role="assistant", text=reply_text))
        db.commit()


__all__ = ["bot", "dp", "router"]
