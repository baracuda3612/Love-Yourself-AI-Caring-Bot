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
from app.orchestrator import handle_incoming_message, session_memory
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
    from app.access import AccessDenied, require_access
    user = db.query(User).filter(User.tg_id == tg_user.id).with_for_update().first()
    if user is None:
        raise AccessDenied()
    require_access(db, user.id)
    user.username = tg_user.username
    user.first_name = tg_user.first_name
    db.commit()
    return user, False


def _sanitize_message_text(text: Optional[str]) -> str:
    if text and text.strip():
        return text
    return "..."


@router.message(Command("start"))
async def cmd_start(message: Message):
    from app.access import AccessDenied, redeem_handoff
    args = message.text.split(maxsplit=1)[1].strip() if message.text and " " in message.text else ""
    if not message.from_user or message.chat.type != "private" or message.chat.id != message.from_user.id:
        return
    try:
        with SessionLocal() as db:
            if args:
                user, created = redeem_handoff(db, args, message.from_user)
                db.commit()
            else:
                user, created = _ensure_user(db, message.from_user)
    except AccessDenied:
        await message.answer("Доступ зараз недоступний. Відкрий актуальне особисте посилання зі сторінки входу.")
        return
    await message.answer("Привіт! Доступ підтверджено. Давай познайомимось." if created else "З поверненням! Продовжуємо.")
    await _show_privacy(message, user.id)


# Gate all Telegram entrances before text, callbacks, Coach or history writes.
# Start alone may redeem a token; it implements the same guard above.
from aiogram import BaseMiddleware


class EnrollmentMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        from app.access import AccessDenied, require_access
        message = event if isinstance(event, Message) else getattr(event, 'message', None)
        actor = getattr(event, 'from_user', None)
        if not actor or not message or message.chat.type != 'private' or message.chat.id != actor.id:
            return
        if isinstance(event, Message) and (event.text or ' ').split(' ', 1)[0].split('@')[0] in {'/start','/privacy','/data_request','/request_status'}:
            return await handler(event, data)
        try:
            with SessionLocal() as db:
                user = db.query(User).filter(User.tg_id == actor.id).first()
                if user is None:
                    raise AccessDenied()
                require_access(db, user.id)
        except AccessDenied:
            await event.answer("Доступ зараз недоступний.")
            return
        return await handler(event, data)


router.message.outer_middleware(EnrollmentMiddleware())
router.callback_query.outer_middleware(EnrollmentMiddleware())


@dp.startup()
async def register_public_commands(bot: Bot):
    from aiogram.types import BotCommand
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Почати або повернутися"),
            BotCommand(command="continue_plan", description="Розблокувати продовження після підсумку"),
        ])
    except Exception:
        logger.exception("Could not update Telegram command menu")


@router.message(Command("continue_plan"))
async def handle_continue_plan(message: Message):
    import asyncio
    from app.plan_completion.workflow import deliver_report
    from app.db import AIPlan, PlanCompletion
    from sqlalchemy.orm import aliased

    def find_source():
        with SessionLocal() as db:
            successor = aliased(AIPlan)
            return (db.query(PlanCompletion.plan_id, User.id).join(AIPlan, AIPlan.id == PlanCompletion.plan_id)
                .join(successor, successor.id == PlanCompletion.successor_plan_id)
                .join(User, User.id == AIPlan.user_id)
                .filter(User.tg_id == message.from_user.id, User.is_active.is_(True),
                    successor.status.in_(['active','paused']),
                    PlanCompletion.state.in_(['uncertain', 'release_uncertain', 'failed', 'sent_held']))
                .order_by(PlanCompletion.plan_id.desc()).first())
    source = await asyncio.to_thread(find_source)
    if source is None:
        await message.answer('Немає продовження, яке потребує розблокування.')
        return
    result = await deliver_report(source[1], source[0], release_operation_id=f'telegram:continue:{message.chat.id}:{message.message_id}')
    if not result.succeeded:
        await message.answer('Продовження ще заблоковане. Спробуй /continue_plan через 10 хвилин після попередньої спроби. Підсумок повторно не надсилаємо.')


@router.message(Command("spawn"))
async def cmd_spawn(message: Message):
    if not message.from_user or message.from_user.id not in settings.ADMIN_IDS:
        return
    await message.answer(
        "Admin task spawning is disabled: active plan structure is immutable."
    )


async def _show_privacy(message, user_id):
    from html import escape
    from app.privacy import pinned_notice, SUPPORT_CONTACT
    from app.access import AccessDenied
    try:
        with SessionLocal() as db:
            deployment, notice = pinned_notice(db, user_id)
            body, version, notice_id = notice.notice_text, notice.version, notice.id
            contact = deployment.support_contact_ref or SUPPORT_CONTACT
    except AccessDenied:
        await message.answer("Повідомлення про приватність поки недоступне для цього доступу. "
            "Для доступу до даних, копії, виправлення чи видалення: /data_request access, "
            "export, correction або deletion. Підтримка: " + SUPPORT_CONTACT)
        return
    await message.answer(escape(body) + "\n\nВерсія: " + escape(version),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="Ознайомився / ознайомилася", callback_data=f"privacy_ack:{notice_id}")]]))
    await message.answer("Права щодо даних: /data_request access, /data_request export, "
        "/data_request correction, /data_request deletion. Статус: /request_status. "
        + ("Відповідальний контакт: " + escape(contact) if contact else "Контакт підтримки ще не активовано."))


@router.message(Command("privacy"))
async def privacy_notice(message: Message):
    with SessionLocal() as db:
        user = db.query(User).filter_by(tg_id=message.from_user.id).first()
        if user is None:
            await message.answer("Дані недоступні.")
            return
        user_id = user.id
    await _show_privacy(message, user_id)


@router.callback_query(F.data.startswith("privacy_ack:"))
async def privacy_ack(callback: CallbackQuery):
    from app.privacy import acknowledge_notice
    from app.access import AccessDenied
    try:
        notice_id = int(callback.data.split(':',1)[1])
        with SessionLocal() as db:
            user = db.query(User).filter_by(tg_id=callback.from_user.id).first()
            if user is None:
                raise AccessDenied()
            acknowledge_notice(db, user.id, notice_id,
                f"telegram:notice:{callback.message.chat.id}:{callback.message.message_id}:{callback.id}")
            db.commit()
    except (AccessDenied, ValueError):
        await callback.answer("Повідомлення змінилося або доступ недоступний. Відкрий /privacy.")
        return
    await callback.answer("Ознайомлення з цією версією записано.")


@router.message(Command("data_request"))
async def data_request(message: Message):
    from app.privacy import receive_request, REQUEST_KINDS
    from app.access import AccessDenied
    args = (message.text or '').split()
    if len(args) not in {2,3} or args[1] not in REQUEST_KINDS:
        await message.answer("Вкажи вид: /data_request access, export, correction або deletion. "
            "Не надсилай документи чи чутливі подробиці в цей чат.")
        return
    try:
        with SessionLocal() as db:
            row = receive_request(db, tg_id=message.from_user.id, kind=args[1],
                source_operation_id=f"telegram:rights:{message.chat.id}:{message.message_id}",
                case_id=args[2] if len(args)==3 else None)
            db.commit()
            receipt = f"Запит {row.id} отримано. Строк відповіді: {row.due_at.date().isoformat()}."
        await message.answer(receipt)
    except (AccessDenied, ValueError):
        await message.answer("Запит не записано: підтримка зараз недоступна. Спробуй пізніше.")


@router.message(Command("request_status"))
async def request_status(message: Message):
    from app.db import PrivacyRequest
    with SessionLocal() as db:
        user = db.query(User).filter_by(tg_id=message.from_user.id).first()
        rows = [] if user is None else db.query(PrivacyRequest).filter_by(user_id=user.id).order_by(
            PrivacyRequest.received_at.desc()).limit(10).all()
        status_names = {'received':'отримано','deleting':'видалення триває','completed':'виконано'}
        reply = '\n'.join(f"{r.id}: {status_names[r.state]}, строк {r.due_at.date().isoformat()}" for r in rows)
    await message.answer(reply or "Активних записів запитів немає.")


@router.message(F.text)
async def on_text(message: Message):
    from app.privacy import pinned_notice, NoticeRequired
    from app.access import AccessDenied

    text = message.text or ""
    try:
        with SessionLocal() as db:
            user, _ = _ensure_user(db, message.from_user)
            user_id = user.id
            pinned_notice(db, user_id, acknowledged=True)
            db.add(ChatHistory(user_id=user_id, role="user", text=text))
            # Log user activity for silence detection.
            log_user_event(
                db,
                user_id=user_id,
                event_type="user_message",
                event_source="telegram",
                source_operation_id=(
                    f"telegram:message:{message.chat.id}:{message.message_id}"
                ),
                context={"message_length": len(message.text or "")},
            )
            db.commit()
    except NoticeRequired:
        # Release ingress access locks before the reminder opens its own
        # session, and before awaiting any Telegram I/O.
        await _show_privacy(message, user_id)
        return
    except AccessDenied:
        await message.answer("Доступ зараз недоступний.")
        return

    response = await handle_incoming_message(user_id, text)
    if not isinstance(response, dict) or "reply_text" not in response:
        raise RuntimeError("handle_incoming_message response must include reply_text")
    await _send_agent_response(message, user_id, response)


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
    from app.scheduler import request_plan_completion
    await asyncio.to_thread(request_plan_completion, transition.user_id, transition.plan_id)
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
    import asyncio
    from app.access import outbound_claim
    if await asyncio.to_thread(outbound_claim, user_id) is None:
        await message.answer("Доступ зараз недоступний.")
        return
    reply_text = _sanitize_message_text(response.get("reply_text"))
    await message.answer(reply_text)
    with SessionLocal() as db:
        from app.access import AccessDenied, require_access
        try:
            require_access(db, user_id)
        except AccessDenied:
            return
        db.add(ChatHistory(user_id=user_id, role="assistant", text=reply_text))
        db.commit()
    await session_memory.append_message(user_id, "assistant", reply_text)


__all__ = ["bot", "dp", "router"]
