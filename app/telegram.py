# app/telegram.py
# Спрощена версія для роботи з новою БД та агентною архітектурою

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional
from weakref import WeakValueDictionary

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.db import AIPlan, AIPlanDay, AIPlanStep, ChatHistory, MenuInput, SessionLocal, User, UserEvent, UserProfile
from app import menu_input, onboarding, user_menu
from app.orchestrator import handle_incoming_message, session_memory
from app.telemetry import log_user_event
from app.lifecycle import (
    LifecycleEntitlementError,
    LifecycleOwnershipError,
    LifecycleTransitionError,
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
    from app.privacy import NoticeRequired, pinned_notice
    args = message.text.split(maxsplit=1)[1].strip() if message.text and " " in message.text else ""
    if not message.from_user or message.chat.type != "private" or message.chat.id != message.from_user.id:
        return
    try:
        with SessionLocal() as db:
            if args:
                user, _ = redeem_handoff(db, args, message.from_user)
                db.commit()
            else:
                user, _ = _ensure_user(db, message.from_user)
            user_id = user.id
            pinned_notice(db, user_id, acknowledged=True)
            pending = db.get(MenuInput, user_id)
            if pending is not None:
                pending.action = None
            progress = onboarding.setup_progress(db, user)
            if progress is not None and progress.stage.startswith("STOPPED_"):
                progress.stage = progress.stage.removeprefix("STOPPED_")
                progress.revision += 1
            screen = onboarding.render(user, progress) if progress is not None else None
            db.commit()
    except NoticeRequired:
        await _open_menu(message, message.from_user.id, "root")
        await _show_privacy(message, user_id)
        return
    except AccessDenied:
        await _open_menu(message, message.from_user.id, "root", denied_start=True)
        return
    await message.answer("Меню", reply_markup=user_menu.keyboard(schedule=screen is None, own_data=True))
    if screen is not None:
        await _send_setup_screen(message, user_id, screen)


@router.message(Command("stop"))
async def stop_setup(message: Message):
    from app.privacy import NoticeRequired, pinned_notice
    from app.access import AccessDenied

    try:
        with SessionLocal() as db:
            user, _ = _ensure_user(db, message.from_user)
            pinned_notice(db, user.id, acknowledged=True)
            progress = onboarding.setup_progress(db, user)
            if progress is not None and not progress.stage.startswith("STOPPED_"):
                progress.stage = f"STOPPED_{progress.stage}"
                progress.revision += 1
            screen = onboarding.render(user, progress) if progress is not None else None
            user_id = user.id
            db.commit()
    except (AccessDenied, NoticeRequired):
        await message.answer("Доступ зараз недоступний.")
        return
    if screen is not None:
        await _send_setup_screen(message, user_id, screen)
    else:
        await _open_menu(message, message.from_user.id, user_menu.SCHEDULE)


# Gate all Telegram entrances before text, callbacks, Coach or history writes.
# Start alone may redeem a token; it implements the same guard above.
from aiogram import BaseMiddleware


class ActorIngressMiddleware(BaseMiddleware):
    """Keep private message/callback routing ordered before asynchronous filters.

    This is an in-process ingress lock for the single polling writer. Durable
    ownership/idempotency remains in PostgreSQL. on_text releases it after its
    routing transaction, so a slow Coach cannot block the user's menu.
    """

    def __init__(self):
        self.locks = WeakValueDictionary()

    async def __call__(self, handler, event, data):
        message = event if isinstance(event, Message) else getattr(event, "message", None)
        actor = getattr(event, "from_user", None)
        if not actor or not message or message.chat.type != "private" or message.chat.id != actor.id:
            return await handler(event, data)
        lock = self.locks.get(actor.id)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[actor.id] = lock
        await lock.acquire()
        held = True

        def release_ingress():
            nonlocal held
            if held:
                held = False
                lock.release()

        data["release_ingress"] = release_ingress
        try:
            return await handler(event, data)
        finally:
            release_ingress()


class EnrollmentMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        from app.access import AccessDenied, require_access
        message = event if isinstance(event, Message) else getattr(event, 'message', None)
        actor = getattr(event, 'from_user', None)
        if not actor or not message or message.chat.type != 'private' or message.chat.id != actor.id:
            return
        if isinstance(event, Message) and event.text in user_menu.MENU_TEXTS:
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and (event.data or "").startswith("menu:"):
            return await handler(event, data)
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


_actor_ingress = ActorIngressMiddleware()
router.message.outer_middleware(_actor_ingress)
router.callback_query.outer_middleware(_actor_ingress)
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

    await _release_continuation(
        message, message.from_user.id, f"telegram:continue:{message.chat.id}:{message.message_id}",
    )


async def _release_continuation(message, telegram_user_id, source_operation_id):
    from app.plan_completion.workflow import deliver_report
    from app.db import AIPlan, PlanCompletion
    from sqlalchemy.orm import aliased

    def find_source():
        with SessionLocal() as db:
            successor = aliased(AIPlan)
            return (db.query(PlanCompletion.plan_id, User.id).join(AIPlan, AIPlan.id == PlanCompletion.plan_id)
                .join(successor, successor.id == PlanCompletion.successor_plan_id)
                .join(User, User.id == AIPlan.user_id)
                .filter(User.tg_id == telegram_user_id, User.is_active.is_(True),
                    successor.status.in_(['active','paused']),
                    PlanCompletion.state.in_(['uncertain', 'release_uncertain', 'failed', 'sent_held']))
                .order_by(PlanCompletion.plan_id.desc()).first())
    source = await asyncio.to_thread(find_source)
    if source is None:
        await message.answer('Немає продовження, яке потребує розблокування.')
        return
    result = await deliver_report(source[1], source[0], release_operation_id=source_operation_id)
    if not result.succeeded:
        await message.answer('Продовження ще заблоковане. Спробуй ще раз через 10 хвилин після попередньої спроби. Підсумок повторно не надсилаємо.')


@router.message(Command("spawn"))
async def cmd_spawn(message: Message):
    if not message.from_user or message.from_user.id not in settings.ADMIN_IDS:
        return
    await message.answer(
        "Admin task spawning is disabled: active plan structure is immutable."
    )


async def _show_privacy(message, user_id):
    from html import escape
    from app.privacy import pinned_notice
    from app.access import AccessDenied
    try:
        with SessionLocal() as db:
            deployment, notice = pinned_notice(db, user_id)
            body, version, notice_id = notice.notice_text, notice.version, notice.id
    except AccessDenied:
        screen = user_menu.navigation(
            "Повідомлення про приватність поки недоступне. Запити щодо своїх даних можна подати в «Мої дані».",
            in_setup=False, actions=(("Мої дані", user_menu.DATA), ("Підтримка", user_menu.SUPPORT)),
        )
        await message.answer(screen.text, reply_markup=screen.keyboard)
        return
    await message.answer(escape(body) + "\n\nВерсія: " + escape(version),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="Ознайомився / ознайомилася", callback_data=f"privacy_ack:{notice_id}")]]))
    await message.answer(
        "Дані й підтримка доступні в меню.",
        reply_markup=user_menu.navigation(
            "", in_setup=False,
            actions=(("Мої дані", user_menu.DATA), ("Підтримка", user_menu.SUPPORT)),
        ).keyboard,
    )


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
            user_id = user.id
            progress = onboarding.setup_progress(db, user)
            screen = onboarding.render(user, progress) if progress is not None else None
            db.commit()
    except (AccessDenied, ValueError):
        await callback.answer("Повідомлення змінилося або доступ недоступний. Відкрий «Конфіденційність» у розділі «Про проєкт».")
        return
    await callback.answer("Ознайомлення з цією версією записано.")
    await callback.message.answer(
        "Меню", reply_markup=user_menu.keyboard(schedule=screen is None, own_data=True),
    )
    if screen is not None:
        await _send_setup_screen(callback.message, user_id, screen)


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


@router.message(F.text.in_(user_menu.MENU_TEXTS))
async def menu_text(message: Message):
    await _open_menu(message, message.from_user.id, message.text, from_menu=True)


@router.callback_query(F.data.startswith("menu:"))
async def menu_action(callback: CallbackQuery):
    await callback.answer()
    await _open_menu(callback.message, callback.from_user.id, callback.data.removeprefix("menu:"),
                     operation_id=f"telegram:menu:{callback.id}", from_menu=True)


async def _open_menu(message, tg_id, route, *, operation_id=None, denied_start=False, hint="", from_menu=False):
    from html import escape

    from app.access import AccessDenied, environment, may_access, require_access
    from app.db import Deployment, DeploymentEnrollment, Organization, PrivacyRequest
    from app.lifecycle import (
        CurrentMode, abandon_current_plan, activate_plan, derive_current_mode, find_lifecycle_operation,
        get_current_plan, switch_plan_format, transition_current_plan,
    )
    from app.plan_drafts.plan_builder_v5 import NoCandidatesError
    from app.plan_finalization import FinalizationError
    from app.privacy import NoticeRequired, pinned_notice, receive_request

    if message.chat.type != "private" or message.chat.id != tg_id:
        return

    result = None
    release_continuation = False
    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == tg_id).with_for_update().first()
        deployment = None
        ready = False
        notice_required = False
        if user is not None:
            deployment = (
                db.query(Deployment)
                .join(DeploymentEnrollment)
                .filter(DeploymentEnrollment.user_id == user.id, Deployment.environment == environment())
                .order_by(DeploymentEnrollment.enrolled_at.desc(), DeploymentEnrollment.id.desc())
                .first()
            )
            try:
                active_deployment, notice = pinned_notice(db, user.id, acknowledged=True)
                deployment = active_deployment
                ready = True
            except NoticeRequired:
                notice_required = True
            except AccessDenied:
                pass

        notice_available = ready
        if notice_required and route in {"help:notice", "help_notice", "privacy"}:
            try:
                pinned_notice(db, user.id)
                notice_available = True
            except AccessDenied:
                pass

        in_setup = ready and derive_current_mode(db, user.id) is CurrentMode.ONBOARDING
        schedule = ready and not in_setup
        own_data = deployment is not None
        markup = None
        show_notice = False
        user_id = user.id if user is not None else None

        def page(text, actions=()):
            return user_menu.navigation(text, in_setup=in_setup, actions=actions)

        prompt = db.get(MenuInput, user_id) if user_id is not None and (
            from_menu or route == "input_pending"
        ) else None
        if from_menu and prompt is not None and not route.startswith(("cal:", "confirm:")) and (
            not route.startswith("input_cancel:") or route == f"input_cancel:{prompt.nonce}"
        ):
            prompt.action = None

        if route.startswith("input_cancel:"):
            if prompt is not None and prompt.action is not None:
                route = "input_pending"
            else:
                route = "setup" if in_setup else "root"

        def ask_for_input(action, body, *, plan=None):
            pending = menu_input.open_prompt(
                db, user, notice.id, action, floor=message.message_id, plan=plan,
            )
            return page(body, (("Скасувати", f"input_cancel:{pending.nonce}"),))

        if route == "input_pending" and prompt is not None and prompt.action is not None:
            screen = menu_input.pending_screen(user, prompt, in_setup=in_setup)
        elif route == "setup" and in_setup:
            screen = onboarding.render(user, onboarding.setup_progress(db, user))
        elif route in {"root", "setup"}:
            markup = user_menu.keyboard(schedule=schedule, own_data=own_data)
            screen = onboarding.SetupScreen(
                "Доступ зараз недоступний. Відкрий актуальне особисте посилання зі сторінки входу."
                if denied_start else "Меню"
            )
        elif route == user_menu.SUPPORT:
            screen = user_menu.support(in_setup)
        elif route == user_menu.ABOUT or route.startswith("help:"):
            screen = user_menu.help_screen(
                route, in_setup=in_setup, notice_access=notice_available, feedback_access=ready,
            )
        elif route.startswith("cal:") and ready and in_setup and operation_id:
            try:
                with db.begin_nested():
                    screen, result, hint = menu_input.calendar_action(db, user, None, prompt, notice.id, route)
            except LifecycleTransitionError:
                screen, hint = None, "Не вдалося змінити часовий пояс."
            if screen is None:
                screen = page(hint or "Налаштування збережено.")
        elif route == "setup_zone" and ready and in_setup and operation_id:
            progress = onboarding.setup_progress(db, user)
            pending = menu_input.open_prompt(
                db, user, notice.id, "setup_zone", floor=message.message_id,
            )
            pending.target_version = progress.revision
            screen = menu_input.calendar_screen(user, pending)
        elif route == "release_continuation" and ready and operation_id:
            release_continuation = True
            screen = page("Оновлюємо стан нагадувань.")
        elif route.startswith("profile_zone:") and schedule and operation_id:
            latest = db.query(AIPlan).filter_by(user_id=user.id).order_by(AIPlan.cycle_number.desc()).first()
            if latest is not None and latest.status == "abandoned" and route == f"profile_zone:{latest.id}:{latest.version}":
                pending = menu_input.open_prompt(
                    db, user, notice.id, "profile_zone", floor=message.message_id, plan=latest,
                )
                screen = menu_input.calendar_screen(user, pending)
            else:
                screen = page("Розклад уже змінився.")
        elif route == "feedback" and ready and operation_id:
            screen = ask_for_input("feedback", user_menu.feedback_text())
        elif route == "edit_data" and ready:
            actions = [("Як до тебе звертатися", "edit_name")]
            current_plan = get_current_plan(db, user.id) if schedule else None
            if current_plan is not None:
                actions.append(("Часовий пояс", f"plan:zone:{current_plan.id}:{current_plan.version}"))
            elif in_setup:
                actions.append(("Часовий пояс", "setup_zone"))
            elif schedule:
                latest = db.query(AIPlan).filter_by(user_id=user.id).order_by(AIPlan.cycle_number.desc()).first()
                if latest is not None and latest.status == "abandoned":
                    actions.append(("Часовий пояс", f"profile_zone:{latest.id}:{latest.version}"))
            actions.append(("Назад до моїх даних", user_menu.DATA))
            screen = page("Виправити дані", actions)
        elif route == "edit_name" and ready and operation_id:
            screen = ask_for_input("name", "Як до тебе звертатися? Надішли ім’я одним повідомленням.")
        elif route == user_menu.SCHEDULE or route.startswith(("plan:", "cal:", "confirm:", "followup:")):
            if schedule:
                plan = get_current_plan(db, user.id, lock=True)
                calendar_plan = plan
                if plan is None and route.startswith("cal:") and prompt is not None:
                    latest = db.query(AIPlan).filter_by(user_id=user.id).order_by(AIPlan.cycle_number.desc()).first()
                    if latest is not None and latest.status == "abandoned" and latest.id == prompt.target_plan_id:
                        calendar_plan = latest
                screen = None
                if route.startswith("confirm:") and operation_id:
                    nonce = route.removeprefix("confirm:")
                    current_prompt = (
                        prompt is not None and prompt.nonce == nonce
                        and prompt.action in {
                            "confirm_end", "done_end", "confirm_short", "confirm_medium",
                            "done_short", "done_medium", "confirm_followup", "done_followup",
                        }
                        and prompt.notice_id == notice.id
                        and prompt.enrollment_id == require_access(db, user.id).id
                        and prompt.expires_at > datetime.now(timezone.utc)
                    )
                    action = prompt.action if current_prompt else None
                    replay = action in {"done_end", "done_short", "done_medium", "done_followup"}
                    expected = False
                    if current_prompt and plan is not None:
                        expected = (plan.id, plan.version) == (prompt.target_plan_id, prompt.target_version)
                    elif current_prompt and action == "confirm_followup":
                        latest = db.query(AIPlan).filter_by(user_id=user.id).order_by(AIPlan.cycle_number.desc()).first()
                        expected = latest is not None and latest.status == "abandoned" and (
                            latest.id, latest.version
                        ) == (prompt.target_plan_id, prompt.target_version)
                    if current_prompt and (expected or replay):
                        source = f"telegram:confirm:{nonce}"
                        if action in {"confirm_end", "done_end"}:
                            with db.begin_nested():
                                result, _ = abandon_current_plan(db, user_id=user.id, source_operation_id=source)
                                prompt.action = "done_end"
                        elif action in {"confirm_followup", "done_followup"}:
                            prior = db.get(AIPlan, prompt.target_plan_id)
                            try:
                                with db.begin_nested():
                                    slots = user.profile.daily_time_slots
                                    target_type = "MEDIUM" if prior.total_days == 14 else "SHORT"
                                    result = activate_plan(
                                        db, user_id=user.id, plan_type=target_type,
                                        day_time=slots["DAY"],
                                        evening_time=slots["EVENING"] if target_type == "MEDIUM" else None,
                                        source_operation_id=f"telegram:followup:{prior.id}:{prompt.target_version}",
                                        require_plan_history=True, required_previous_status="abandoned",
                                    )
                                    prompt.action = "done_followup"
                            except (LifecycleTransitionError, FinalizationError, NoCandidatesError):
                                hint = "Не вдалося створити розклад. Спробуй ще раз."
                        elif action in {"confirm_short", "confirm_medium", "done_short", "done_medium"}:
                            target_type = "MEDIUM" if action.endswith("medium") else "SHORT"
                            format_source = menu_input.format_operation(
                                prompt.target_plan_id, prompt.target_version, target_type,
                            )
                            try:
                                with db.begin_nested():
                                    result = switch_plan_format(
                                        db, user_id=user.id, target_plan_type=target_type,
                                        source_operation_id=format_source,
                                    )
                                    prompt.action = "done_medium" if target_type == "MEDIUM" else "done_short"
                            except (LifecycleTransitionError, FinalizationError, NoCandidatesError):
                                hint = "Не вдалося змінити формат. Спробуй ще раз."
                            if result is not None and result.code == "needs_evening_time":
                                screen = ask_for_input(
                                    "switch_evening",
                                    "Введи час вечірнього нагадування у форматі ГГ:ХХ, наприклад 18:30.",
                                    plan=plan,
                                )
                                result = None
                    else:
                        hint = "Ця кнопка вже неактуальна."
                elif route.startswith("followup:") and plan is None and operation_id:
                    latest = db.query(AIPlan).filter_by(user_id=user.id).order_by(AIPlan.cycle_number.desc()).first()
                    if latest is not None and latest.status == "abandoned" and route == f"followup:{latest.id}:{latest.version}":
                        pending = menu_input.open_prompt(
                            db, user, notice.id, "confirm_followup", floor=message.message_id, plan=latest,
                        )
                        screen = page(
                            f"Почати нові {latest.total_days} робочих днів за збереженим розкладом?",
                            (("Підтвердити", f"confirm:{pending.nonce}"), ("Скасувати", user_menu.SCHEDULE)),
                        )
                elif route.startswith("cal:") and operation_id:
                    try:
                        with db.begin_nested():
                            screen, result, hint = menu_input.calendar_action(
                                db, user, calendar_plan, prompt, notice.id, route,
                            )
                    except LifecycleTransitionError:
                        hint = "Не вдалося змінити розклад. Спробуй ще раз або звернися до підтримки."
                elif route.startswith("plan:") and operation_id:
                    action, _, target = route.removeprefix("plan:").partition(":")
                    expected = f"{plan.id}:{plan.version}" if plan is not None else None
                    source = f"telegram:{route}"
                    if action in {"pause", "resume"}:
                        receipt = find_lifecycle_operation(db, user.id, source)
                        if target == expected or receipt is not None:
                            try:
                                with db.begin_nested():
                                    result = transition_current_plan(
                                        db, user_id=user.id, operation=action, source_operation_id=source,
                                    )
                            except LifecycleTransitionError:
                                hint = "Ця дія зараз недоступна."
                        else:
                            hint = "Розклад уже змінився."
                    elif action == "settings" and target == expected:
                        settings_actions = [("Змінити час", f"plan:day:{target}")]
                        if plan.total_days == 14 and user.profile and user.profile.evening_slot_collected:
                            settings_actions.append(("Змінити вечірній час", f"plan:evening:{target}"))
                        settings_actions.extend((
                            ("Дні надсилання", f"plan:days:{target}"),
                            ("Часовий пояс", f"plan:zone:{target}"),
                            ("Назад до розкладу", user_menu.SCHEDULE),
                        ))
                        screen = page("Що змінити в розкладі?", settings_actions)
                    elif action in {"days", "zone"} and target == expected:
                        from app.active_days import resolve_active_days

                        pending = menu_input.open_prompt(
                            db, user, notice.id, f"calendar_{action}", floor=message.message_id, plan=plan,
                        )
                        if action == "days":
                            accepted = resolve_active_days(user.profile)
                            pending.draft_days = sum(1 << i for i, day in enumerate(onboarding.DAYS) if day in accepted)
                        screen = menu_input.calendar_screen(user, pending)
                    elif action == "format" and target == expected:
                        target_type = "short" if plan.total_days == 14 else "medium"
                        days = 7 if target_type == "short" else 14
                        pending = menu_input.open_prompt(
                            db, user, notice.id, f"confirm_{target_type}",
                            floor=message.message_id, plan=plan,
                        )
                        screen = page(
                            f"Перейти на {days} робочих днів? Поточний розклад завершиться, і почнеться новий.",
                            (("Підтвердити зміну", f"confirm:{pending.nonce}"),
                             ("Скасувати", user_menu.SCHEDULE)),
                        )
                    elif action in {"day", "evening"} and target == expected:
                        if action == "day" or (
                            plan.total_days == 14 and user.profile and user.profile.evening_slot_collected
                        ):
                            label = "вечірнього нагадування" if action == "evening" else "нагадування"
                            screen = ask_for_input(
                                action, f"Введи новий час {label} у форматі ГГ:ХХ, наприклад 14:30.",
                                plan=plan,
                            )
                    elif action == "confirm_end" and target == expected:
                        pending = menu_input.open_prompt(
                            db, user, notice.id, "confirm_end",
                            floor=message.message_id, plan=plan,
                        )
                        screen = page(
                            "Завершити поточний розклад?",
                            (("Підтвердити завершення", f"confirm:{pending.nonce}"),
                             ("Скасувати", user_menu.SCHEDULE)),
                        )
                if screen is None and result is None:
                    actions = []
                    if plan is not None:
                        from app.db import PlanCompletion

                        blocked = db.query(PlanCompletion.plan_id).filter(
                            PlanCompletion.successor_plan_id == plan.id,
                            PlanCompletion.state.in_(("uncertain", "release_uncertain", "failed", "sent_held")),
                        ).first()
                        if blocked is not None:
                            actions.append(("Продовжити нагадування", "release_continuation"))
                        target = f"{plan.id}:{plan.version}"
                        actions.extend((
                            ("Змінити налаштування", f"plan:settings:{target}"),
                            ("Змінити формат", f"plan:format:{target}"),
                        ))
                        if plan.status == "paused":
                            actions.append(("Відновити", f"plan:resume:{target}"))
                        else:
                            actions.append(("Призупинити", f"plan:pause:{target}"))
                        actions.append(("Завершити", f"plan:confirm_end:{target}"))
                    if plan is None:
                        latest = db.query(AIPlan).filter_by(user_id=user.id).order_by(AIPlan.cycle_number.desc()).first()
                        if latest is not None and latest.status == "abandoned":
                            actions.append((f"Почати нові {latest.total_days} днів", f"followup:{latest.id}:{latest.version}"))
                    body = user_menu.schedule_text(
                        db, user, plan, delivery=not hint and may_access(db, user.id, delivery=True),
                    )
                    screen = page(body, actions)
            else:
                markup = user_menu.keyboard(schedule=False, own_data=own_data)
                screen = onboarding.SetupScreen("Розклад зараз недоступний.")
        elif route == user_menu.DATA and own_data:
            body = "<b>Мої дані</b>"
            if ready:
                profile_name = user.profile.name_preference if user.profile else None
                name = profile_name or user.first_name or "не вказано"
                organization = db.get(Organization, deployment.organization_id)
                body += (
                    f"\n\nІм’я: {escape(name)}"
                    f"\nЧасовий пояс: {escape(onboarding.zone_label(user.timezone))}"
                    f"\nКомпанія: {escape(organization.display_name)}\nДоступ: чинний"
                )
            else:
                body += "\n\nДоступ: " + ("потрібне ознайомлення з приватністю" if notice_required else "неактивний")
            edit = (("Виправити дані", "edit_data"),) if ready else ()
            screen = page(body, edit + (
                ("Отримати копію даних", "export"),
                ("Видалити мої дані", "delete"), ("Мої звернення", "requests"),
            ))
        elif route in {"help_notice", "privacy"}:
            show_notice = notice_available
            screen = user_menu.help_screen(
                "help:notice", in_setup=in_setup, notice_access=show_notice,
            )
        elif route == "delete" and own_data:
            screen = page(
                "Подати запит на видалення даних? Підтримка опрацює звернення.",
                (("Підтвердити запит", "deletion"), ("Скасувати", user_menu.DATA)),
            )
        elif route in {"export", "deletion"} and own_data and operation_id:
            try:
                row = receive_request(db, tg_id=tg_id, kind=route, source_operation_id=operation_id)
                screen = page(f"Запит {row.id} отримано. Строк відповіді: {row.due_at:%d.%m.%Y}.")
            except AccessDenied:
                screen = page("Запит не записано. Спробуй пізніше або звернися до підтримки.")
        elif route == "requests" and own_data:
            rows = (
                db.query(PrivacyRequest).filter_by(user_id=user.id)
                .order_by(PrivacyRequest.received_at.desc()).limit(10).all()
            )
            statuses = {"received": "отримано", "deleting": "видалення триває", "completed": "виконано"}
            body = "\n".join(
                f"{row.id}: {statuses[row.state]}, строк {row.due_at:%d.%m.%Y}" for row in rows
            )
            screen = page(body or "Звернень поки немає.")
        else:
            markup = user_menu.keyboard(schedule=schedule, own_data=own_data)
            screen = onboarding.SetupScreen("Меню")
        if from_menu and route == user_menu.SCHEDULE and prompt is not None:
            prompt.action = None
        if hint and screen is not None:
            screen = onboarding.SetupScreen(f"{hint}\n\n{screen.text}", screen.keyboard)
        if user is not None:
            db.commit()

    if release_continuation:
        await _release_continuation(message, tg_id, operation_id)
        await _open_menu(message, tg_id, user_menu.SCHEDULE)
        return
    if result is not None:
        hint = await _reconcile_menu_schedule(result)
        await _open_menu(message, tg_id, user_menu.SCHEDULE, hint=hint)
        return
    if show_notice:
        await _show_privacy(message, user_id)
    await message.answer(screen.text, reply_markup=markup or screen.keyboard)


async def _reconcile_menu_schedule(result):
    from app.lifecycle_reconciliation import reconcile_scheduler_effects
    from app.plan_runtime.tools import _finish_plan_format_result

    if result.operation == "switch_plan_format":
        outcome = await asyncio.to_thread(_finish_plan_format_result, result, pending_status="needs_evening_time")
        if outcome.get("status") != "ok":
            return "Не вдалося підтвердити оновлення розкладу."
        return ""
    outcome = await asyncio.to_thread(reconcile_scheduler_effects, result)
    if outcome.external_effects_succeeded:
        return ""
    hint = "Оновлення надсилання ще не підтверджено."
    if result.operation == "change_schedule_calendar":
        hint += " Відкрий «Змінити налаштування» й повторно застосуй ті самі дні або часовий пояс."
    return hint


@router.message(F.text)
async def on_text(message: Message, release_ingress=None):
    from app.privacy import pinned_notice, NoticeRequired
    from app.access import AccessDenied
    from app.plan_drafts.plan_builder_v5 import NoCandidatesError
    from app.plan_finalization import FinalizationError

    text = message.text or ""
    try:
        with SessionLocal() as db:
            user, _ = _ensure_user(db, message.from_user)
            user_id = user.id
            _, notice = pinned_notice(db, user_id, acknowledged=True)
            screen = None
            setup_row = user.onboarding_progress
            setup_duplicate = setup_row is not None and message.message_id <= setup_row.last_text_message_id
            try:
                with db.begin_nested():
                    submitted = (
                        menu_input.InputResult("setup", "") if setup_duplicate else menu_input.handle_text(
                            db, user, notice.id, message.message_id, text, sent_at=getattr(message, "date", None),
                        )
                    )
            except (LifecycleTransitionError, FinalizationError, NoCandidatesError):
                submitted = menu_input.InputResult(
                    user_menu.SCHEDULE, "Не вдалося змінити розклад. Спробуй ще раз.",
                )
            if submitted is None:
                progress = onboarding.setup_progress(db, user)
                if progress is not None:
                    screen = onboarding.handle_text(user, progress, text)
                    progress.last_text_message_id = message.message_id
                else:
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

    if release_ingress is not None:
        release_ingress()

    if submitted is not None:
        hint = submitted.hint
        if submitted.effect is not None:
            hint = await _reconcile_menu_schedule(submitted.effect) or hint
        await _open_menu(message, message.from_user.id, submitted.route, hint=hint)
        return
    if screen is not None:
        await _send_setup_screen(message, user_id, screen)
        return

    response = await handle_incoming_message(user_id, text)
    if not isinstance(response, dict) or "reply_text" not in response:
        raise RuntimeError("handle_incoming_message response must include reply_text")
    await _send_agent_response(message, user_id, response)

async def _send_setup_screen(message, user_id, screen):
    from app.access import outbound_claim

    if await asyncio.to_thread(outbound_claim, user_id) is None:
        await message.answer("Доступ зараз недоступний.")
        return
    await message.answer(screen.text, reply_markup=screen.keyboard)


def _setup_activation_screen(user_id, result):
    from zoneinfo import ZoneInfo

    from app.access import may_access
    from app.lifecycle_reconciliation import reconcile_scheduler_effects
    from app.privacy import pinned_notice

    outcome = reconcile_scheduler_effects(result)
    if outcome.external_effects_succeeded:
        with SessionLocal() as db:
            pinned_notice(db, user_id, acknowledged=True)
            user = db.get(User, user_id)
            first = (
                db.query(AIPlanStep.scheduled_for, AIPlanStep.step_status)
                .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
                .join(AIPlan, AIPlan.id == AIPlanDay.plan_id)
                .filter(
                    AIPlan.id == result.plan_id,
                    AIPlan.user_id == user_id,
                    AIPlan.status == "active",
                    AIPlanStep.scheduled_for.is_not(None),
                )
                .order_by(AIPlanStep.scheduled_for)
                .first()
            )
            if (
                first is not None and first.step_status == "pending"
                and first.scheduled_for > datetime.now(timezone.utc)
                and user.timezone and may_access(db, user_id, delivery=True)
            ):
                local = first.scheduled_for.astimezone(ZoneInfo(user.timezone))
                return onboarding.SetupScreen(
                    f"Готово. Перше планове нагадування прийде <b>{local:%d.%m.%Y} о {local:%H:%M}</b>."
                )
    return onboarding.SetupScreen("Розклад створено. Поки не можемо підтвердити час першого нагадування.")


@router.callback_query(F.data.startswith("setup:"))
async def setup_action(callback: CallbackQuery):
    from app.access import AccessDenied
    from app.plan_drafts.plan_builder_v5 import NoCandidatesError
    from app.plan_finalization import FinalizationError
    from app.privacy import NoticeRequired, pinned_notice

    parts = callback.data.split(":", 2)
    if len(parts) != 3 or not parts[1].isascii() or not parts[1].isdigit() or len(parts[1]) > 19:
        await callback.answer("Ця кнопка вже неактуальна.")
        return
    _, revision, action = parts
    result = None
    try:
        with SessionLocal() as db:
            user = db.query(User).filter_by(tg_id=callback.from_user.id).with_for_update().first()
            if user is None:
                raise AccessDenied()
            user_id = user.id
            pinned_notice(db, user_id, acknowledged=True)
            progress = onboarding.setup_progress(db, user)
            if progress is None:
                screen = onboarding.SetupScreen("Налаштування вже завершено.")
                result = onboarding.recover_confirmation(db, user, int(revision), action)
            else:
                screen, result = onboarding.handle_action(db, user, progress, int(revision), action)
            db.commit()
    except NoticeRequired:
        await callback.answer()
        await _show_privacy(callback.message, user_id)
        return
    except (AccessDenied, LifecycleEntitlementError):
        await callback.answer("Доступ зараз недоступний.")
        return
    except (FinalizationError, NoCandidatesError, LifecycleTransitionError):
        logger.exception("Setup activation failed for user %s", user_id)
        await callback.answer("Не вдалося створити розклад. Спробуй підтвердити ще раз.", show_alert=True)
        return

    await callback.answer()
    if result is not None:
        try:
            screen = await asyncio.to_thread(_setup_activation_screen, user_id, result)
        except NoticeRequired:
            await _show_privacy(callback.message, user_id)
            return
        except AccessDenied:
            await callback.message.answer("Доступ зараз недоступний.")
            return
    if result is not None:
        await _open_menu(callback.message, callback.from_user.id, "root")
    await _send_setup_screen(callback.message, user_id, screen)


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
