# app/telegram.py
# Спрощена версія для роботи з новою БД та агентною архітектурою

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.db import AIPlan, AIPlanDay, AIPlanStep, ChatHistory, SessionLocal, User, UserProfile
from app.orchestrator import (
    PLAN_GENERATION_WAIT_MESSAGE,
    build_plan_draft_preview,
    handle_incoming_message,
    session_memory,
)
from app.plan_guards import validate_step_action
from app.session_memory import SessionMemory
from app.scheduler import schedule_plan_step
from app.redis_client import create_fsm_storage, create_redis_client
from app.telemetry import log_user_event
from app.logging.router_logging import log_metric

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


@router.message(Command("spawn"))
async def cmd_spawn(message: Message):
    if not message.from_user or message.from_user.id not in settings.ADMIN_IDS:
        return

    parts = (message.text or "").strip().split()
    if len(parts) != 4:
        await message.answer("Usage: /spawn <count> <interval_seconds> <start_offset_seconds>")
        return

    try:
        count = int(parts[1])
        interval_seconds = int(parts[2])
        start_offset_seconds = int(parts[3])
    except ValueError:
        await message.answer("Usage: /spawn <count> <interval_seconds> <start_offset_seconds>")
        return

    if count < 1:
        await message.answer("Count must be >= 1")
        return
    if count > 20:
        await message.answer("Max 20 tasks per spawn.")
        return
    if interval_seconds < 0 or start_offset_seconds < 0:
        await message.answer("interval_seconds and start_offset_seconds must be >= 0")
        return

    with SessionLocal() as db:
        user, _ = _ensure_user(db, message.from_user)
        if user.current_state != "ACTIVE":
            await message.answer("Spawn not allowed: user state is not ACTIVE.")
            return

        active_plan = (
            db.query(AIPlan)
            .filter(AIPlan.user_id == user.id, AIPlan.status == "active")
            .order_by(AIPlan.id.desc())
            .first()
        )
        if not active_plan:
            await message.answer("Spawn not allowed: no active plan.")
            return

        plan_day = (
            db.query(AIPlanDay)
            .filter(AIPlanDay.plan_id == active_plan.id)
            .order_by(AIPlanDay.day_number.asc())
            .first()
        )
        if not plan_day:
            await message.answer("Spawn not allowed: active plan has no days.")
            return

        now_utc = datetime.now(timezone.utc)
        first_run_at = now_utc + timedelta(seconds=start_offset_seconds)
        scheduled_at = first_run_at
        created_steps = 0

        max_order = (
            db.query(AIPlanStep.order_in_day)
            .filter(AIPlanStep.day_id == plan_day.id)
            .order_by(AIPlanStep.order_in_day.desc())
            .first()
        )
        next_order = (max_order[0] if max_order and max_order[0] is not None else 0) + 1

        for index in range(count):
            step = AIPlanStep(
                day_id=plan_day.id,
                title=f"Admin spawned task #{index + 1}",
                description=(
                    "This is a scheduled test task created by /spawn command."
                ),
                order_in_day=next_order + index,
                scheduled_for=scheduled_at,
            )
            db.add(step)
            db.flush()

            if schedule_plan_step(step, user):
                created_steps += 1

            scheduled_at = scheduled_at + timedelta(seconds=interval_seconds)

        db.commit()

    log_metric(
        "admin_spawn_tasks",
        extra={"admin_tg_id": message.from_user.id, "count": count, "scheduled_jobs": created_steps},
    )
    await message.answer(
        f"Spawned {count} tasks.\n"
        f"First run at: {first_run_at.isoformat()}\n"
        f"Plan ID: {active_plan.id}"
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

        is_allowed, error_msg = validate_step_action(step)
        if not is_allowed:
            await callback_query.answer(error_msg)
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
        await callback_query.message.answer("✅ Завдання відмічено як виконане.")


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

        is_allowed, error_msg = validate_step_action(step)
        if not is_allowed:
            await callback_query.answer(error_msg)
            return

        step.skipped = True
        step.is_completed = False
        step.completed_at = None

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
        await callback_query.message.answer("⏭️ Завдання відмічено як пропущене.")


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
