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
from app.db import AIPlan, AIPlanDay, AIPlanStep, ChatHistory, SessionLocal, User, UserEvent, UserProfile
from app.orchestrator import (
    PLAN_GENERATION_WAIT_MESSAGE,
    build_plan_draft_preview,
    handle_incoming_message,
    session_memory,
)
from app.plan_guards import validate_step_action
from app.scheduler import schedule_plan_step
from app.ux.catalog import get_trigger_message
from app.ux.persona import get_persona
from app.ux.task_notification import get_step_rationale
from app.redis_client import create_fsm_storage, create_redis_client
from app.telemetry import get_success_streak, log_user_event
from app.logging.router_logging import log_metric

bot = Bot(token=settings.BOT_TOKEN, parse_mode="HTML")
redis_client = create_redis_client()
storage = create_fsm_storage(redis_client) or MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)
logger = logging.getLogger(__name__)

_PLAN_ACTIONS = [
    ("✅ Confirm plan", "plan_confirm", "підтвердь план"),
    ("🔁 Regenerate", "plan_regenerate", "перегенеруй план"),
    ("✏️ Change parameters", "plan_edit", "зміни параметри"),
    ("🔄 Restart from scratch", "plan_restart", "почни спочатку"),
]

_ADAPTATION_ACTIONS = [
    ("✅ Підтвердити", "adapt_confirm", "підтверджую"),
    ("✏️ Змінити параметр", "adapt_edit_params", "змінити параметр"),
    ("🔀 Інша адаптація", "adapt_change_type", "хочу вибрати іншу адаптацію"),
    ("❌ Скасувати", "adapt_cancel", "скасувати"),
]


def _build_plan_action_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=callback)]
            for label, callback, _ in _PLAN_ACTIONS
        ]
    )


def _build_adaptation_action_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=cb)]
            for label, cb, _ in _ADAPTATION_ACTIONS
        ]
    )


def _adaptation_action_text(callback_data: str) -> str:
    for _, cb, text in _ADAPTATION_ACTIONS:
        if cb == callback_data:
            return text
    return "скасувати"


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
        effective_offset = max(start_offset_seconds, 5)
        first_run_at = now_utc + timedelta(seconds=effective_offset)
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
        "Spawned tasks.\n"
        f"Requested: count={count}, interval_seconds={interval_seconds}, start_offset_seconds={start_offset_seconds}\n"
        f"Effective offset: {effective_offset}s\n"
        f"First run at (UTC): {first_run_at.isoformat()}\n"
        f"Plan ID: {active_plan.id}\n"
        f"Scheduled jobs: {created_steps}"
    )


@router.message(F.text)
async def on_text(message: Message):
    text = message.text or ""
    with SessionLocal() as db:
        user, _ = _ensure_user(db, message.from_user)
        db.add(ChatHistory(user_id=user.id, role="user", text=text))
        # Log user activity for silence detection
        # This event is read by check_silent_users() in scheduler.py
        log_user_event(
            db,
            user_id=user.id,
            event_type="user_message",
            context={"message_length": len(message.text or "")},
        )
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


@router.callback_query(F.data.in_([action[1] for action in _ADAPTATION_ACTIONS]))
async def on_adaptation_action(callback_query: CallbackQuery):
    cb_data = callback_query.data or ""
    message_text = _adaptation_action_text(cb_data)

    with SessionLocal() as db:
        user, _ = _ensure_user(db, callback_query.from_user)
        db.add(ChatHistory(user_id=user.id, role="user", text=message_text))
        db.commit()

    await callback_query.answer()
    response = await handle_incoming_message(user.id, message_text, defer_plan_draft=True)
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

        try:
            with SessionLocal() as db:
                step = db.query(AIPlanStep).filter(AIPlanStep.id == step_id).first()
                if not step:
                    return

                day = step.day
                plan = day.plan
                user = plan.user
                persona = get_persona(user.profile)
                streak = get_success_streak(db, user.id)
                rationale = get_step_rationale(db, step)

                all_today = db.query(AIPlanStep).filter(
                    AIPlanStep.day_id == day.id,
                    AIPlanStep.canceled_by_adaptation == False,
                ).all()
                all_done = all(s.is_completed for s in all_today)

                total_completed = db.query(UserEvent).filter(
                    UserEvent.user_id == user.id,
                    UserEvent.event_type == "task_completed",
                ).count()
                last_two = db.query(UserEvent).filter(
                    UserEvent.user_id == user.id,
                    UserEvent.event_type.in_(["task_completed", "task_skipped"]),
                ).order_by(UserEvent.timestamp.desc()).limit(2).all()
                prev_event = last_two[1] if len(last_two) > 1 else None

                is_comeback = prev_event and prev_event.event_type == "task_skipped"
                is_first = total_completed == 1

                if is_comeback:
                    trigger_id = "comeback_after_skip"
                elif is_first:
                    trigger_id = "first_task_ever"
                elif streak == 3:
                    trigger_id = "streak_3"
                elif streak == 7:
                    trigger_id = "streak_7"
                elif all_done:
                    trigger_id = "day_all_done"
                else:
                    trigger_id = "task_completed"

                context = {
                    "name": user.first_name,
                    "exercise": step.title,
                    "day": day.day_number,
                    "streak": streak,
                    "focus": getattr(plan, "focus", None),
                    "rationale": rationale,
                }
                msg = get_trigger_message(trigger_id, persona, context)
        except Exception:
            logger.exception("Failed to build completion trigger message")
            msg = None

        if msg:
            await callback_query.message.answer(msg, parse_mode="HTML")
        else:
            await callback_query.message.answer("✅ Виконано!")


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

        try:
            with SessionLocal() as db:
                step = db.query(AIPlanStep).filter(AIPlanStep.id == step_id).first()
                if not step:
                    return

                user = step.day.plan.user
                persona = get_persona(user.profile)
                recent_actions = db.query(UserEvent).filter(
                    UserEvent.user_id == user.id,
                    UserEvent.event_type.in_(["task_completed", "task_skipped"]),
                ).order_by(UserEvent.timestamp.desc()).limit(2).all()
                two_skips = len(recent_actions) >= 2 and all(e.event_type == "task_skipped" for e in recent_actions)
                trigger_id = "skip_2_in_row" if two_skips else "task_skipped"
                context = {"name": user.first_name, "exercise": step.title, "day": step.day.day_number}
                msg = get_trigger_message(trigger_id, persona, context)
        except Exception:
            logger.exception("Failed to build skip trigger message")
            msg = None
            trigger_id = "task_skipped"

        keyboard = None
        if trigger_id == "skip_2_in_row":
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🔧 Переглянути план", callback_data="adapt_suggest")]]
            )

        if msg:
            await callback_query.message.answer(msg, parse_mode="HTML", reply_markup=keyboard)
        else:
            await callback_query.message.answer("⏭️ Пропущено", reply_markup=keyboard)


@router.callback_query(F.data == "adapt_suggest")
async def handle_adapt_suggest(callback_query: CallbackQuery):
    await callback_query.answer()
    with SessionLocal() as db:
        user, _ = _ensure_user(db, callback_query.from_user)

    response = await handle_incoming_message(
        user.id,
        "хочу переглянути план через пропуски",
    )
    if callback_query.message:
        await _send_agent_response(callback_query.message, user.id, response)


async def _send_agent_response(message: Message, user_id: int, response: dict) -> None:
    # Читаємо актуальний стан юзера один раз на початку.
    # Orchestrator вже зробив FSM transition і commit до цього виклику.
    with SessionLocal() as db:
        _state_row = db.query(User.current_state).filter(User.id == user_id).first()
        _user_state = _state_row[0] if _state_row else None

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
        if response.get("show_plan_actions"):
            reply_markup = _build_plan_action_keyboard()
        elif _user_state == "ADAPTATION_CONFIRMATION":
            reply_markup = _build_adaptation_action_keyboard()
        else:
            reply_markup = None
        await message.answer(preview_text, reply_markup=reply_markup)
        with SessionLocal() as db:
            db.add(ChatHistory(user_id=user_id, role="assistant", text=preview_text))
            db.commit()
        await session_memory.append_message(user_id, "assistant", preview_text)
        return

    reply_text = _sanitize_message_text(response.get("reply_text"))
    if response.get("show_plan_actions"):
        reply_markup = _build_plan_action_keyboard()
    elif _user_state == "ADAPTATION_CONFIRMATION":
        reply_markup = _build_adaptation_action_keyboard()
    else:
        reply_markup = None
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
