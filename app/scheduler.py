# Новий файл app/scheduler.py
import asyncio
import os
from datetime import datetime
from typing import Optional

import pytz
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.ai import generate_daily_message
from app.config import DB_URL, DEFAULT_SEND_HOUR, MODEL
from app.db import (
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

jobstores = {
    'default': SQLAlchemyJobStore(url=jobstore_url)
}

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
    return CronTrigger(hour=hour, minute=0, timezone=tz)


async def _send_daily(user_id: int):
    # Виконується всередині головного циклу
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


async def _send_custom_reminder(user_id: int, message: str, reminder_id: int | None = None):
    from app.telegram import bot as tg_bot

    with SessionLocal() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return
        try:
            await tg_bot.send_message(user.tg_id, message)
        except Exception:
            return

        if reminder_id is not None:
            reminder = db.query(UserReminder).filter(UserReminder.id == reminder_id).first()
            if reminder and not reminder.cron_expression:
                reminder.active = False
            db.commit()


def schedule_daily_delivery(user: User):
    if not user.active:
        return

    job_id = f"daily_{user.id}"

    def _job():
        if _event_loop is None:
            return
        asyncio.run_coroutine_threadsafe(_send_daily(user.id), _event_loop)

    trigger = _cron_for_user(user)
    scheduler.add_job(_job, trigger, id=job_id, replace_existing=True)


def restore_custom_reminder(reminder: UserReminder):
    if not reminder.active:
        return

    job_id = reminder.job_id
    if scheduler.get_job(job_id):
        return

    def _job():
        if _event_loop is None:
            return
        asyncio.run_coroutine_threadsafe(
            _send_custom_reminder(reminder.user_id, reminder.message, reminder.id),
            _event_loop,
        )

    if reminder.cron_expression:
        try:
            trigger = CronTrigger.from_crontab(
                reminder.cron_expression, timezone=reminder.timezone or "UTC"
            )
        except ValueError:
            return
        scheduler.add_job(_job, trigger, id=job_id, replace_existing=True)
    elif reminder.run_at and reminder.run_at > datetime.now(pytz.UTC):
        scheduler.add_job(
            _job,
            "date",
            id=job_id,
            run_date=reminder.run_at.astimezone(pytz.UTC),
            replace_existing=True,
        )


async def schedule_daily_loop():
    global _scheduler_started, _event_loop

    if _scheduler_started:
        return

    _scheduler_started = True
    _event_loop = asyncio.get_running_loop()

    init_scheduler()

    with SessionLocal() as db:
        users = db.query(User).filter(User.active == True).all()
        for user in users:
            schedule_daily_delivery(user)

        reminders = (
            db.query(UserReminder)
            .filter(UserReminder.active == True)
            .all()
        )
        for reminder in reminders:
            restore_custom_reminder(reminder)

    await asyncio.Event().wait()
