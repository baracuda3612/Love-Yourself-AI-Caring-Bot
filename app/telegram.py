# app/telegram.py
# Спрощена версія для роботи з новою БД та агентною архітектурою

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.db import AIPlan, AIPlanDay, AIPlanStep, ChatHistory, SessionLocal, User, UserEvent, UserProfile
from app.orchestrator import (
    PLAN_GENERATION_WAIT_MESSAGE,
    SLOT_RANGES,
    _build_time_select_keyboard,
    _build_task_select_keyboard,
    build_plan_draft_preview,
    handle_incoming_message,
    session_memory,
)
from app.plan_guards import validate_step_action
from app.ux.catalog import get_trigger_message
from app.ux.persona import get_persona
from app.ux.task_notification import get_step_rationale
from app.redis_client import create_fsm_storage, create_redis_client
from app.telemetry import get_success_streak, log_user_event
from app.lifecycle import (
    CurrentMode,
    LifecycleTransitionError,
    derive_current_mode,
    ensure_onboarding_progress,
    transition_plan_step,
)

bot = Bot(
    token=settings.BOT_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML"),
)
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

_SCHEDULE_ADJ_TIMEOUT_CALLBACKS = [
    "sched_adj_timeout_reset",
    "sched_adj_timeout_continue",
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
        )
        db.add(user)
        db.flush()
        ensure_onboarding_progress(db, user.id, stage="START")
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
    args = message.text.split(maxsplit=1)[1] if message.text and " " in message.text else ""

    if args.startswith("newplan_"):
        await _handle_newplan_deeplink(message, args)
        return

    with SessionLocal() as db:
        user, is_created = _ensure_user(db, message.from_user)
        if is_created:
            await message.answer("Привіт! Я LoveYourself бот. Давай познайомимось.")
        else:
            await message.answer("З поверненням! Продовжуємо.")
    logger.info("User %s started. Created: %s", user.id, is_created)


async def _handle_newplan_deeplink(message: Message, args: str) -> None:
    from app.orchestrator import handle_incoming_message

    parts = args.split("_")
    if len(parts) != 4:
        await message.answer("Некоректне посилання. Напиши мені напряму.")
        return

    _, duration, load, focus = parts
    tg_id = message.from_user.id

    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == tg_id).first()
        if not user:
            await message.answer("Спочатку потрібно зареєструватись.")
            return
        if derive_current_mode(db, user.id) is not CurrentMode.NO_ACTIVE_PLAN:
            await message.answer(
                "Зараз не можу розпочати новий план. "
                "Заверши поточний або напиши — розберемось."
            )
            return
        internal_id = user.id

    response = await handle_incoming_message(
        user_id=internal_id, message_text="створити план"
    )
    if response.get("reply_text"):
        await message.answer(response["reply_text"])


@router.message(Command("spawn"))
async def cmd_spawn(message: Message):
    if not message.from_user or message.from_user.id not in settings.ADMIN_IDS:
        return
    await message.answer(
        "Admin task spawning is disabled: active plan structure is immutable."
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
            event_source="telegram",
            source_operation_id=(
                f"telegram:message:{message.chat.id}:{message.message_id}"
            ),
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


@router.callback_query(F.data.startswith("sched_task:"))
async def on_sched_adj_task(callback_query: CallbackQuery):
    value = (callback_query.data or "").removeprefix("sched_task:")
    with SessionLocal() as db:
        user, _ = _ensure_user(db, callback_query.from_user)

    await callback_query.answer()
    if callback_query.message:
        await callback_query.message.edit_reply_markup(reply_markup=None)

    if value == "CANCEL":
        response = await handle_incoming_message(user.id, "скасувати зміну часу")
        if callback_query.message:
            await _send_agent_response(callback_query.message, user.id, response)
        return

    if value == "MULTI":
        ctx = await session_memory.get_schedule_adjustment_context(user.id) or {}
        all_slots = list(ctx.get("active_tasks", {}).keys())
        await session_memory.update_schedule_adjustment_context(
            user.id,
            {
                "slots_queue": all_slots,
                "current_slot": all_slots[0] if all_slots else None,
                "step": "time_select",
            },
        )
        if callback_query.message and all_slots:
            ctx = await session_memory.get_schedule_adjustment_context(user.id) or {}
            active_tasks = ctx.get("active_tasks", {})
            first_slot = all_slots[0]
            current_time = active_tasks.get(first_slot, "")
            keyboard = _build_time_select_keyboard(first_slot, current_time, in_multi=True)
            await callback_query.message.answer(
                f"Починаємо. Завдання зараз о {current_time} — вибери новий час:",
                reply_markup=keyboard,
            )
        return

    slot = value
    ctx = await session_memory.get_schedule_adjustment_context(user.id) or {}
    active_tasks = ctx.get("active_tasks", {})
    current_time = active_tasks.get(slot, "")
    await session_memory.update_schedule_adjustment_context(user.id, {"current_slot": slot, "step": "time_select", "slots_queue": []})

    if callback_query.message:
        keyboard = _build_time_select_keyboard(slot, current_time, in_multi=False)
        await callback_query.message.answer(
            f"Завдання зараз о {current_time} — вибери новий час:",
            reply_markup=keyboard,
        )


@router.callback_query(F.data.startswith("sched_time:"))
async def on_sched_adj_time(callback_query: CallbackQuery):
    cb_data = (callback_query.data or "").removeprefix("sched_time:")
    parts = cb_data.split(":", 1)
    if len(parts) != 2:
        return
    slot, value = parts

    with SessionLocal() as db:
        user, _ = _ensure_user(db, callback_query.from_user)

    await callback_query.answer()
    if callback_query.message:
        await callback_query.message.edit_reply_markup(reply_markup=None)

    if value == "CANCEL":
        response = await handle_incoming_message(user.id, "скасувати зміну часу")
        if callback_query.message:
            await _send_agent_response(callback_query.message, user.id, response)
        return

    if value == "CUSTOM":
        await session_memory.update_schedule_adjustment_context(user.id, {"current_slot": slot, "step": "time_select"})
        if callback_query.message:
            slot_start = SLOT_RANGES[slot][0].strftime("%H:%M")
            slot_end = SLOT_RANGES[slot][1].strftime("%H:%M")
            await callback_query.message.answer(f"Введи час ({slot_start}–{slot_end}):")
        return

    if value == "ONLY_THIS":
        await session_memory.update_schedule_adjustment_context(
            user.id,
            {
                "current_slot": slot,
                "slots_queue": [],
                "step": "time_select",
            },
        )
        ctx = await session_memory.get_schedule_adjustment_context(user.id) or {}
        current_time = ctx.get("active_tasks", {}).get(slot, "")
        keyboard = _build_time_select_keyboard(slot, current_time, in_multi=False)
        if callback_query.message:
            await callback_query.message.answer(
                "Добре, змінюємо тільки це завдання. Вибери час:",
                reply_markup=keyboard,
            )
        return

    await session_memory.update_schedule_adjustment_context(user.id, {"current_slot": slot})
    response = await handle_incoming_message(user.id, f"обираю {value}")
    if callback_query.message:
        await _send_agent_response(callback_query.message, user.id, response)


@router.callback_query(F.data.in_(_SCHEDULE_ADJ_TIMEOUT_CALLBACKS))
async def on_sched_adj_timeout(callback_query: CallbackQuery):
    cb_data = callback_query.data or ""
    with SessionLocal() as db:
        user, _ = _ensure_user(db, callback_query.from_user)

        if cb_data == "sched_adj_timeout_reset":
            ctx = await session_memory.get_schedule_adjustment_context(user.id) or {}
            plan_was_paused = bool(ctx.get("plan_was_paused", False))
            # Legacy tunnel storage is inert. Plan status already preserves
            # active/paused lifecycle truth.
            await session_memory.clear_schedule_adjustment_context(user.id)
            await session_memory.clear_schedule_adjustment_last_active(user.id)
            await session_memory.clear_schedule_adjustment_soft_prompted(user.id)
            await callback_query.answer()
            if callback_query.message:
                await callback_query.message.edit_reply_markup(reply_markup=None)
            await bot.send_message(user.tg_id, "Повертаємось до плану. Час завдань залишається без змін. 👍")

        elif cb_data == "sched_adj_timeout_continue":
            await session_memory.set_schedule_adjustment_last_active(user.id)
            await session_memory.clear_schedule_adjustment_soft_prompted(user.id)
            await callback_query.answer()
            if callback_query.message:
                await callback_query.message.edit_reply_markup(reply_markup=None)

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
            # Expired steps fail silently — buttons just stop responding.
            if step.step_status in ("expired", "canceled"):
                await callback_query.answer()
            else:
                await callback_query.answer(error_msg)
            return

        try:
            transition = transition_plan_step(
                db,
                user_id=step.day.plan.user_id,
                step_id=step.id,
                target_status="completed",
                source_operation_id=f"telegram:{callback_query.id}:complete:{step.id}",
            )
        except LifecycleTransitionError:
            await callback_query.answer("Завдання вже завершене")
            return
        if transition.duplicate:
            db.rollback()
            await callback_query.answer("Завдання вже завершене")
            return

        log_user_event(
            db,
            user_id=step.day.plan.user_id,
            event_type="task_completed",
            event_source="telegram",
            source_operation_id=(
                f"telegram:{callback_query.id}:complete:{step.id}"
            ),
            plan_step_id=step.id,
            context={"day_number": step.day.day_number},
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
                ).all()
                all_done = all(s.step_status == "completed" for s in all_today)

                total_completed = db.query(UserEvent).filter(
                    UserEvent.user_id == user.id,
                    UserEvent.event_name == "task_completed",
                ).count()
                last_two = db.query(UserEvent).filter(
                    UserEvent.user_id == user.id,
                    UserEvent.event_name.in_(["task_completed", "task_skipped"]),
                ).order_by(UserEvent.occurred_at.desc()).limit(2).all()
                prev_event = last_two[1] if len(last_two) > 1 else None

                is_comeback = prev_event and prev_event.event_name == "task_skipped"
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
            if step.step_status in ("expired", "canceled"):
                await callback_query.answer()
            else:
                await callback_query.answer(error_msg)
            return

        try:
            transition = transition_plan_step(
                db,
                user_id=step.day.plan.user_id,
                step_id=step.id,
                target_status="skipped",
                source_operation_id=f"telegram:{callback_query.id}:skip:{step.id}",
            )
        except LifecycleTransitionError:
            await callback_query.answer("Завдання вже завершене")
            return
        if transition.duplicate:
            db.rollback()
            await callback_query.answer("Завдання вже завершене")
            return

        log_user_event(
            db,
            user_id=step.day.plan.user_id,
            event_type="task_skipped",
            event_source="telegram",
            source_operation_id=f"telegram:{callback_query.id}:skip:{step.id}",
            plan_step_id=step.id,
            context={"day_number": step.day.day_number},
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
                    UserEvent.event_name.in_(["task_completed", "task_skipped"]),
                ).order_by(UserEvent.occurred_at.desc()).limit(2).all()
                two_skips = len(recent_actions) >= 2 and all(
                    event.event_name == "task_skipped" for event in recent_actions
                )
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
        else:
            reply_markup = None
        await message.answer(preview_text, reply_markup=reply_markup)
        with SessionLocal() as db:
            db.add(ChatHistory(user_id=user_id, role="assistant", text=preview_text))
            db.commit()
        await session_memory.append_message(user_id, "assistant", preview_text)
        return

    reply_text = _sanitize_message_text(response.get("reply_text"))
    keyboard = response.get("keyboard")
    if keyboard is not None:
        reply_markup = keyboard
    elif response.get("show_plan_actions"):
        reply_markup = _build_plan_action_keyboard()
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
