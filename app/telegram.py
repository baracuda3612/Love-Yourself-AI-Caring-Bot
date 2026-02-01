# app/telegram.py
# Спрощена версія для роботи з новою БД та агентною архітектурою

import asyncio
import logging
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

from app.config import settings
from app.db import ChatHistory, SessionLocal, User, UserProfile
from app.orchestrator import (
    PLAN_GENERATION_WAIT_MESSAGE,
    build_plan_draft_preview,
    handle_incoming_message,
    session_memory,
)
from app.session_memory import SessionMemory
from app.redis_client import create_fsm_storage, create_redis_client

bot = Bot(token=settings.BOT_TOKEN, parse_mode="HTML")
redis_client = create_redis_client()
storage = create_fsm_storage(redis_client) or MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)
logger = logging.getLogger(__name__)
session_memory = SessionMemory(limit=20)


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


def _sanitize_message_text(text: Optional[str]) -> str:
    if text and text.strip():
        return text
    return "..."


@router.message(Command("start"))
async def cmd_start(message: Message):
    with SessionLocal() as db:
        user, is_created = _ensure_user(db, message.from_user)
        if is_created:
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

    response = await handle_incoming_message(user.id, text, defer_plan_draft=True)
    if not isinstance(response, dict) or "reply_text" not in response:
        raise RuntimeError("handle_incoming_message response must include reply_text")
    if response.get("defer_plan_draft"):
        wait_text = _sanitize_message_text(PLAN_GENERATION_WAIT_MESSAGE)
        await message.answer(wait_text)
        with SessionLocal() as db:
            db.add(ChatHistory(user_id=user.id, role="assistant", text=wait_text))
            db.commit()
        await session_memory.append_message(user.id, "assistant", wait_text)

        await asyncio.sleep(5.5)

        preview_text = await build_plan_draft_preview(
            user.id,
            response.get("plan_draft_parameters") or {},
        )
        preview_text = _sanitize_message_text(preview_text)
        await message.answer(preview_text)
        with SessionLocal() as db:
            db.add(ChatHistory(user_id=user.id, role="assistant", text=preview_text))
            db.commit()
        await session_memory.append_message(user.id, "assistant", preview_text)
        return

    reply_text = _sanitize_message_text(response.get("reply_text"))
    await message.answer(reply_text)

    with SessionLocal() as db:
        db.add(ChatHistory(user_id=user.id, role="assistant", text=reply_text))
        db.commit()

    followup_messages = response.get("followup_messages") or []
    for followup in followup_messages:
        followup_text = _sanitize_message_text(followup)
        await message.answer(followup_text)
        await session_memory.append_message(user.id, "assistant", followup_text)
        with SessionLocal() as db:
            db.add(ChatHistory(user_id=user.id, role="assistant", text=followup_text))
            db.commit()


__all__ = ["bot", "dp", "router"]
