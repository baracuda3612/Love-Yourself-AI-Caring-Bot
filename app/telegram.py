# app/telegram.py
# Спрощена версія для роботи з новою БД та агентною архітектурою

import logging
from datetime import datetime, timezone
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.db import AIPlanStep, ChatHistory, SessionLocal, User, UserEvent, UserProfile
from app.orchestrator import handle_incoming_message
from app.telemetry import log_user_event
from app.lifecycle import (
    CurrentMode,
    LifecycleEntitlementError,
    LifecycleOwnershipError,
    LifecycleTransitionError,
    derive_current_mode,
    ensure_onboarding_progress,
    transition_owned_plan_step,
)

bot = Bot(
    token=settings.BOT_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML"),
)
dp = Dispatcher(disable_fsm=True)
router = Router()
dp.include_router(router)
logger = logging.getLogger(__name__)


async def _clear_terminal_callback_keyboard(callback_query: CallbackQuery, step_id: int) -> bool:
    """Remove buttons once; a remaining button is the user's explicit retry path."""
    message = callback_query.message
    if message is None:
        return False
    try:
        await message.edit_reply_markup(reply_markup=None)
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            logger.exception("Terminal keyboard removal failed step=%s", step_id)
            try:
                await message.answer(
                    "⚠️ Стан вправи збережено, але кнопки не вдалося прибрати. "
                    "Натисни їх ще раз, щоб повторити прибирання; "
                    "вправа вдруге не зарахується."
                )
            except Exception:
                logger.exception("Could not notify button cleanup failure step=%s", step_id)
            return False
    message_id = getattr(message, "message_id", None)
    if message_id is None:
        return True
    try:
        with SessionLocal() as db:
            step = (
                db.query(AIPlanStep)
                .filter(
                    AIPlanStep.id == step_id,
                    AIPlanStep.tg_message_id == message_id,
                    AIPlanStep.step_status.in_(
                        ("completed", "skipped", "expired", "canceled")
                    ),
                )
                .first()
            )
            if step is not None:
                step.tg_message_id = None
                db.commit()
    except Exception:
        logger.exception("Terminal keyboard receipt update pending step=%s", step_id)
    return True


def _stale_terminal_keyboard_status(
    callback_query: CallbackQuery, step_id: int
) -> str | None:
    """Authorize a stale-button retry without repeating the recorded action."""
    message_id = getattr(callback_query.message, "message_id", None)
    if message_id is None:
        return None
    try:
        with SessionLocal() as db:
            step = db.query(AIPlanStep).filter(AIPlanStep.id == step_id).first()
            if (
                step is None
                or step.step_status not in {"completed", "skipped", "expired", "canceled"}
                or step.tg_message_id != message_id
                or step.day.plan.user.tg_id != callback_query.from_user.id
            ):
                return None
            status = str(step.step_status)
    except Exception:
        logger.exception("Stale button lookup failed step=%s", step_id)
        return None
    return status


async def _answer_stale_terminal_callback(
    callback_query: CallbackQuery, step_id: int
) -> bool:
    status = _stale_terminal_keyboard_status(callback_query, step_id)
    if status is None:
        return False
    replies = {
        "completed": "Завдання вже виконано",
        "skipped": "Завдання вже пропущено",
        "expired": "Термін завдання минув",
        "canceled": "Завдання скасовано",
    }
    await callback_query.answer(replies[status])
    await _clear_terminal_callback_keyboard(callback_query, step_id)
    return True


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

    response = await handle_incoming_message(user.id, text)
    if not isinstance(response, dict) or "reply_text" not in response:
        raise RuntimeError("handle_incoming_message response must include reply_text")
    await _send_agent_response(message, user.id, response)


_STEP_REPLIES = {
    "completed": "Виконано", "skipped": "Пропущено",
    "expired": "Час дії завершився", "canceled": "Скасовано",
}


async def _project_step_status(step_id):
    import asyncio
    from app.scheduler import reconcile_terminal_step_keyboards
    return await asyncio.to_thread(reconcile_terminal_step_keyboards, [step_id])


async def _handle_step_action(callback_query: CallbackQuery, target: str):
    import asyncio
    received_at = datetime.now(timezone.utc)
    try:
        step_id = int((callback_query.data or '').split(':')[1])
    except (ValueError, IndexError):
        await callback_query.answer("Завдання не знайдено")
        return
    def commit_action():
        with SessionLocal() as db:
            transition = transition_owned_plan_step(
                db, telegram_user_id=callback_query.from_user.id, step_id=step_id,
                target_status=target, source_operation_id=f"telegram:{callback_query.id}:{target}:{step_id}",
                telegram_message=callback_query.message,
                occurred_at=received_at,
            )
            db.commit()
            return transition
    try:
        transition = await asyncio.to_thread(commit_action)
    except LifecycleOwnershipError:
        await callback_query.answer("Це не ваше завдання")
        return
    except LifecycleEntitlementError:
        await callback_query.answer("Дія зараз недоступна")
        return
    except LifecycleTransitionError as exc:
        replies = {"plan_step_missing": "Завдання не знайдено", "plan_not_active": "План зараз не активний",
                   "step_not_delivered": "Доставку ще не підтверджено"}
        await callback_query.answer(replies.get(str(exc), "Дія зараз недоступна"))
        return
    except Exception:
        logger.exception("Exercise action failed step=%s", step_id)
        await callback_query.answer("Не вдалося зберегти дію. Спробуй ще раз")
        return
    await callback_query.answer(_STEP_REPLIES[transition.status])
    await _project_step_status(step_id)


@router.callback_query(F.data.startswith("task_complete:"))
async def handle_task_completed(callback_query: CallbackQuery):
    await _handle_step_action(callback_query, "completed")


@router.callback_query(F.data.startswith("task_skip:"))
async def handle_task_skipped(callback_query: CallbackQuery):
    await _handle_step_action(callback_query, "skipped")


@router.callback_query(F.data.startswith("task_feedback:"))
async def handle_task_feedback(callback_query: CallbackQuery):
    import asyncio
    from app.lifecycle import submit_step_feedback
    try:
        _, identity, value = (callback_query.data or '').split(':')
        step_id = int(identity)
    except (ValueError, IndexError):
        await callback_query.answer("Некоректний відгук")
        return
    def commit_feedback():
        with SessionLocal() as db:
            recorded, duplicate = submit_step_feedback(db, telegram_user_id=callback_query.from_user.id,
                                                       step_id=step_id, value=value)
            db.commit()
            return recorded, duplicate
    try:
        recorded, duplicate = await asyncio.to_thread(commit_feedback)
    except LifecycleOwnershipError:
        await callback_query.answer("Це не ваша вправа")
        return
    except (LifecycleEntitlementError, LifecycleTransitionError):
        await callback_query.answer("Відгук доступний після виконання")
        return
    except Exception:
        logger.exception("Exercise feedback failed step=%s", step_id)
        await callback_query.answer("Не вдалося зберегти відгук. Спробуй ще раз")
        return
    label = {"better": "краще", "same": "так само", "worse": "гірше"}[recorded]
    await callback_query.answer(f"Відгук збережено: {label}")
    await _project_step_status(step_id)


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
    reply_text = _sanitize_message_text(response.get("reply_text"))
    await message.answer(reply_text)

    with SessionLocal() as db:
        db.add(ChatHistory(user_id=user_id, role="assistant", text=reply_text))
        db.commit()


__all__ = ["bot", "dp", "router"]
