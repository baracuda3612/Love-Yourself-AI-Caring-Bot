# app/telegram.py
# Спрощена версія для роботи з новою БД та агентною архітектурою

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.db import AIPlanStep, ChatHistory, SessionLocal, User, UserProfile
from app.orchestrator import (
    PLAN_GENERATION_WAIT_MESSAGE,
    build_plan_draft_preview,
    handle_incoming_message,
    session_memory,
)
from app.session_memory import SessionMemory
from app.redis_client import create_fsm_storage, create_redis_client
from app.telemetry import log_user_event

bot = Bot(token=settings.BOT_TOKEN, parse_mode="HTML")
redis_client = create_redis_client()
storage = create_fsm_storage(redis_client) or MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)
logger = logging.getLogger(__name__)
session_memory = SessionMemory(limit=20)

_PLAN_ACTIONS = [
    ("✅ Confirm plan", "plan_confirm", "підтвердь план"),
    ("🔁 Regenerate", "plan_regenerate", "перегенеруй план"),
    ("✏️ Change parameters", "plan_edit", "зміни параметри"),
    ("🔄 Restart from scratch", "plan_restart", "почни спочатку"),
]


def _build_plan_action_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=callback)]
            for label, callback, _ in _PLAN_ACTIONS
        ]
    )


def _plan_action_text(callback_data: str) -> str:
    for _, callback, text in _PLAN_ACTIONS:
        if callback == callback_data:
            return text
    return callback_data


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
    await _send_agent_response(message, user.id, response)


@router.callback_query(F.data.in_([action[1] for action in _PLAN_ACTIONS]))
async def on_plan_action(callback_query: CallbackQuery):
    callback_text = _plan_action_text(callback_query.data or "")
    with SessionLocal() as db:
        user, _ = _ensure_user(db, callback_query.from_user)
        db.add(ChatHistory(user_id=user.id, role="user", text=callback_text))
        db.commit()
    await callback_query.answer()
    response = await handle_incoming_message(user.id, callback_text, defer_plan_draft=True)
    if not isinstance(response, dict) or "reply_text" not in response:
        raise RuntimeError("handle_incoming_message response must include reply_text")
    if callback_query.message:
        await _send_agent_response(callback_query.message, user.id, response)


@router.callback_query(F.data.startswith("task_complete:"))
async def handle_task_completed(callback_query: CallbackQuery):
    """
    User clicked ✅ Виконано button.
    """
    if not callback_query.data:
        await callback_query.answer("Завдання не знайдено")
        return

    step_id = int(callback_query.data.split(":")[1])
    user_id = callback_query.from_user.id

    with SessionLocal() as db:
        step = db.query(AIPlanStep).filter(AIPlanStep.id == step_id).first()
        if not step:
            await callback_query.answer("Завдання не знайдено")
            return

        if step.day.plan.user.tg_id != user_id:
            await callback_query.answer("Це не ваше завдання")
            return

        if step.is_completed:
            await callback_query.answer("Вже виконано")
            return

        step.is_completed = True
        step.completed_at = datetime.now(timezone.utc)
        step.skipped = False

        log_user_event(
            db,
            user_id=step.day.plan.user_id,
            event_type="task_completed",
            plan_step_id=step.id,
            context={
                "exercise_id": step.exercise_id,
                "day_number": step.day.day_number,
            },
        )

        db.commit()

    await callback_query.answer("✅ Чудово! Завдання виконано.")
    if callback_query.message:
        await callback_query.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith("task_skip:"))
async def handle_task_skipped(callback_query: CallbackQuery):
    """
    User clicked ⏭️ Пропустити button.
    """
    if not callback_query.data:
        await callback_query.answer("Завдання не знайдено")
        return

    step_id = int(callback_query.data.split(":")[1])
    user_id = callback_query.from_user.id

    with SessionLocal() as db:
        step = db.query(AIPlanStep).filter(AIPlanStep.id == step_id).first()
        if not step:
            await callback_query.answer("Завдання не знайдено")
            return

        if step.day.plan.user.tg_id != user_id:
            await callback_query.answer("Це не ваше завдання")
            return

        if step.skipped:
            await callback_query.answer("Вже пропущено")
            return

        step.skipped = True
        step.is_completed = False

        log_user_event(
            db,
            user_id=step.day.plan.user_id,
            event_type="task_skipped",
            plan_step_id=step.id,
            context={
                "exercise_id": step.exercise_id,
                "day_number": step.day.day_number,
            },
        )

        db.commit()

    await callback_query.answer("⏭️ Завдання пропущено")
    if callback_query.message:
        await callback_query.message.edit_reply_markup(reply_markup=None)


async def _send_agent_response(message: Message, user_id: int, response: dict) -> None:
    if response.get("defer_plan_draft"):
        wait_text = _sanitize_message_text(PLAN_GENERATION_WAIT_MESSAGE)
        await message.answer(wait_text)
        with SessionLocal() as db:
            db.add(ChatHistory(user_id=user_id, role="assistant", text=wait_text))
            db.commit()
        await session_memory.append_message(user_id, "assistant", wait_text)

        await asyncio.sleep(5.5)

        preview_text = await build_plan_draft_preview(
            user_id,
            response.get("plan_draft_parameters") or {},
        )
        preview_text = _sanitize_message_text(preview_text)
        reply_markup = _build_plan_action_keyboard() if response.get("show_plan_actions") else None
        await message.answer(preview_text, reply_markup=reply_markup)
        with SessionLocal() as db:
            db.add(ChatHistory(user_id=user_id, role="assistant", text=preview_text))
            db.commit()
        await session_memory.append_message(user_id, "assistant", preview_text)
        return

    reply_text = _sanitize_message_text(response.get("reply_text"))
    reply_markup = _build_plan_action_keyboard() if response.get("show_plan_actions") else None
    await message.answer(reply_text, reply_markup=reply_markup)

    with SessionLocal() as db:
        db.add(ChatHistory(user_id=user_id, role="assistant", text=reply_text))
        db.commit()

    followup_messages = response.get("followup_messages") or []
    for followup in followup_messages:
        followup_text = _sanitize_message_text(followup)
        await message.answer(followup_text)
        await session_memory.append_message(user_id, "assistant", followup_text)
        with SessionLocal() as db:
            db.add(ChatHistory(user_id=user_id, role="assistant", text=followup_text))
            db.commit()


__all__ = ["bot", "dp", "router"]
