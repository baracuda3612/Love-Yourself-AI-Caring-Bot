# app/scheduler.py
import asyncio
import os
from datetime import datetime, timedelta
from typing import Optional

import pytz
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.ai import generate_daily_message
from app.config import DB_URL, DEFAULT_SEND_HOUR, MODEL
from app.db import (
    AIPlan,
    AIPlanStep,
    Delivery,
    SessionLocal,
    User,
    UserReminder,
)

# Використаємо окремий файл для jobstore поруч із основною БД (jobs.sqlite)
# Якщо DB_URL = sqlite:///./ly_bot.db -> jobs at ./jobs.sqlite
if DB_URL.startswith("sqlite:///"):
    base_path = DB_URL.replace("sqlite:///", "")
    base_dir = os.path.dirname(os.path.abspath(base_path)) or "."
    jobs_db_path = os.path.join(base_dir, "jobs.sqlite")
    jobstore_url = f"sqlite:///{jobs_db_path}"
else:
    # Якщо Postgres/інші, можна використовувати той же DB_URL
    jobstore_url = DB_URL

jobstores = {"default": SQLAlchemyJobStore(url=jobstore_url)}
scheduler = BackgroundScheduler(jobstores=jobstores, timezone="UTC")

_scheduler_started = False
_event_loop: Optional[asyncio.AbstractEventLoop] = None


def init_scheduler():
    # Запускати при старті програми
    scheduler.start()


def shutdown_scheduler():
    scheduler.shutdown(wait=True)


# Утиліти для додавання/видалення job-ів
def add_job(func, trigger, id=None, **kwargs):
    return scheduler.add_job(func, trigger, id=id, **kwargs)


def remove_job(job_id):
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass


def reschedule_job(job_id, **trigger_args):
    try:
        scheduler.reschedule_job(job_id, **trigger_args)
    except Exception:
        raise


def _cron_for_user(user: User) -> CronTrigger:
    tz = user.timezone or "Europe/Kyiv"
    hour = user.send_hour if user.send_hour is not None else DEFAULT_SEND_HOUR
    return CronTrigger(hour=hour, minute=0, timezone=pytz.timezone(tz))


def _submit_coroutine(coro):
    if _event_loop is None:
        return None
    return asyncio.run_coroutine_threadsafe(coro, _event_loop)


async def _send_message_async(chat_id: int, text: str):
    """Надсилання повідомлення з контексту job-а."""
    from app.telegram import bot as tg_bot
    try:
        return await tg_bot.send_message(chat_id, text)
    except Exception:
        return None


def send_scheduled_message(chat_id: int, text: str, step_id: int | None = None):
    """
    Топ-рівень (можна серіалізувати як 'module:function').
    Якщо переданий step_id — відмічаємо крок виконаним і за потреби закриваємо план.
    """
    result = _submit_coroutine(_send_message_async(chat_id, text))

    if step_id is not None:
        with SessionLocal() as db:
            step = db.query(AIPlanStep).filter(AIPlanStep.id == step_id).first()
            if step:
                step.is_completed = True
                step.status = "completed"
                step.completed_at = datetime.now(pytz.UTC)
                step.job_id = None

                if step.plan:
                    all_done = all(s.is_completed or s.status == "completed" for s in step.plan.steps)
                    if all_done:
                        step.plan.status = "completed"
                        step.plan.completed_at = datetime.now(pytz.UTC)
                db.commit()

    return result


async def _send_daily(user_id: int):
    """Щоденне повідомлення (з кнопками) для користувача."""
    from app.telegram import bot as tg_bot, send_daily_with_buttons

    with SessionLocal() as db:
        user = db.query(User).filter(User.id == user_id, User.active == True).first()
        if not user:
            return

        profile = f"{user.first_name or ''} @{user.username or ''}".strip()
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        message = None
        text = ""

        try:
            text, usage = generate_daily_message(profile or "Користувач", user.prompt_template)
            message = await send_daily_with_buttons(tg_bot, user.tg_id, text)
        except Exception:
            pass

        now_utc = datetime.now(pytz.UTC)
        delivery = Delivery(
            user_id=user.id,
            scheduled_for=now_utc,
            sent_at=now_utc if message else None,
            status="sent" if message else "failed",
            message_id=getattr(message, "message_id", None),
            prompt_snapshot=user.prompt_template,
            model=MODEL if text else None,
            tokens_prompt=usage.get("prompt_tokens", 0),
            tokens_completion=usage.get("completion_tokens", 0),
            tokens_total=usage.get("total_tokens", 0),
        )
        db.add(delivery)
        db.commit()


async def _send_custom_reminder(reminder_id: int):
    with SessionLocal() as db:
        reminder = db.query(UserReminder).filter(UserReminder.id == reminder_id).first()
        if not reminder or not reminder.active:
            return

        user = db.query(User).filter(User.id == reminder.user_id).first()
        if not user:
            return

        message = await _send_message_async(user.tg_id, reminder.message)

        if not reminder.cron_expression:
            # one-shot — деактивуємо після відправки
            reminder.active = False

        db.commit()


# ===== Топ-рівневі callable для APScheduler (ніяких лямбд/замикань) =====

def run_daily_job(user_id: int):
    """Sync wrapper — викликається з jobstore; запускає корутину у головному loop."""
    _submit_coroutine(_send_daily(user_id))


def run_custom_reminder_job(reminder_id: int):
    """Sync wrapper для custom reminder."""
    _submit_coroutine(_send_custom_reminder(reminder_id))


# ===== API для реєстрації задач =====

def schedule_daily_delivery(user: User):
    """Щоденне повідомлення за user.send_hour / timezone."""
    if not user.active:
        return

    job_id = f"daily_{user.id}"
    trigger = _cron_for_user(user)

    # Текстове посилання module:function — серіалізується коректно
    scheduler.add_job(
        "app.scheduler:run_daily_job",
        trigger,
        id=job_id,
        kwargs={"user_id": user.id},
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )


def schedule_custom_reminder(reminder: UserReminder):
    """Одинарне чи cron-нагадування користувачу."""
    if not reminder.active:
        return

    job_id = reminder.job_id

    if reminder.cron_expression:
        try:
            trigger = CronTrigger.from_crontab(
                reminder.cron_expression,
                timezone=pytz.timezone(reminder.timezone or "UTC"),
            )
        except ValueError:
            return

        scheduler.add_job(
            "app.scheduler:run_custom_reminder_job",
            trigger,
            id=job_id,
            kwargs={"reminder_id": reminder.id},
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )

    elif reminder.scheduled_at:
        scheduled_utc = reminder.scheduled_at.astimezone(pytz.UTC)
        if scheduled_utc <= datetime.now(pytz.UTC):
            _submit_coroutine(_send_custom_reminder(reminder.id))
            return

        scheduler.add_job(
            "app.scheduler:run_custom_reminder_job",
            "date",
            id=job_id,
            run_date=scheduled_utc,
            kwargs={"reminder_id": reminder.id},
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )


def schedule_plan_step(step: AIPlanStep, user: User) -> bool:
    """
    Додає job для кроку плану. Повертає True, якщо ми щойно видали новий job_id
    (тобто step був без job_id і ми його поставили).
    """
    if step.is_completed:
        return False
    if step.status and step.status != "approved":
        return False
    if not user or not user.active:
        return False
    if not step.scheduled_for:
        # ще не затверджено/не встановлений час — нема що шедулити
        return False

    new_job_id_assigned = False
    if not step.job_id:
        step.job_id = AIPlanStep.generate_job_id(user.id, step.plan_id)
        new_job_id_assigned = True

    run_date = step.scheduled_for.astimezone(pytz.UTC)
    now_utc = datetime.now(pytz.UTC)
    if run_date <= now_utc:
        run_date = now_utc + timedelta(minutes=1)

    scheduler.add_job(
        "app.scheduler:send_scheduled_message",
        "date",
        id=step.job_id,
        run_date=run_date,
        args=[user.tg_id, step.message, step.id],
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )

    return new_job_id_assigned


async def schedule_daily_loop():
    """
    Ініціалізує scheduler, піднімає loop-проксі і реєструє всі актуальні job-и:
    - daily для активних користувачів,
    - custom reminders,
    - кроки планів зі статусом approved (і з проставленим scheduled_for).
    """
    global _scheduler_started, _event_loop

    if _scheduler_started:
        return

    _scheduler_started = True
    _event_loop = asyncio.get_running_loop()

    init_scheduler()

    with SessionLocal() as db:
        # 1) Daily
        users = db.query(User).filter(User.active == True).all()
        for user in users:
            schedule_daily_delivery(user)

        # 2) Custom reminders
        reminders = db.query(UserReminder).filter(UserReminder.active == True).all()
        for reminder in reminders:
            schedule_custom_reminder(reminder)

        # 3) Кроки планів (approved + не completed + є scheduled_for)
        plan_rows = (
            db.query(AIPlanStep, AIPlan, User)
            .join(AIPlan, AIPlan.id == AIPlanStep.plan_id)
            .join(User, User.id == AIPlan.user_id)
            .filter(
                AIPlan.status == "active",
                AIPlanStep.is_completed == False,
                User.active == True,
            )
            .all()
        )

        dirty = False
        for step, plan, user in plan_rows:
            # legacy: якщо status None — вважаємо approved (щоб не зависали старі кроки)
            if step.status is None:
                step.status = "approved"
                dirty = True

            # шедул лише коли є scheduled_for
            if step.scheduled_for is not None:
                assigned = schedule_plan_step(step, user)
                if assigned:
                    dirty = True

        if dirty:
            db.commit()

    # Тримаємо процес живим
    await asyncio.Event().wait()
