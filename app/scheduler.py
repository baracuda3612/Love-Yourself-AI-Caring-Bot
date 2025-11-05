# app/scheduler.py
import asyncio
import logging
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

logger = logging.getLogger(__name__)

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
    """Send a message from scheduled job context."""
    from app.telegram import bot as tg_bot

    try:
        return await tg_bot.send_message(chat_id, text)
    except Exception:
        return None


def send_scheduled_message(chat_id: int, text: str):
    # Топ-рівень, серіалізується нормально
    return _submit_coroutine(_send_message_async(chat_id, text))


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
            reminder.active = False

        db.commit()


# ===== Топ-рівневі callable для APScheduler (жодних замикань/лямбд) =====

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

    # Використовуємо текстове посилання module:function — серіалізується коректно
    logger.info("Ensuring daily job %s for user %s", job_id, user.id)
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
            logger.warning(
                "Skipping reminder %s for user %s due to invalid cron '%s'",
                reminder.id,
                reminder.user_id,
                reminder.cron_expression,
            )
            return

        logger.info(
            "Restoring cron reminder job %s for user %s", job_id, reminder.user_id
        )
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

        logger.info(
            "Restoring one-shot reminder job %s for user %s at %s",
            job_id,
            reminder.user_id,
            scheduled_utc.isoformat(),
        )
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


async def schedule_daily_loop():
    """Ініціалізує scheduler, піднімає loop-проксі і реєструє всі актуальні job-и."""
    global _scheduler_started, _event_loop

    if _scheduler_started:
        return

    _scheduler_started = True
    _event_loop = asyncio.get_running_loop()

    logger.info("Starting scheduler restoration loop")
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

        # 3) Планові кроки з конкретними датами (one-shot date jobs)
        now_utc = datetime.now(pytz.UTC)
        plan_rows = (
            db.query(AIPlanStep, AIPlan, User)
            .join(AIPlan, AIPlan.id == AIPlanStep.plan_id)
            .join(User, User.id == AIPlan.user_id)
            .filter(
                AIPlan.status.in_(["active"]),
                AIPlanStep.is_completed == False,
                AIPlanStep.status == "approved",
                AIPlanStep.scheduled_for > now_utc,
                User.active == True,
            )
            .all()
        )

        dirty = False
        for step, plan, user in plan_rows:
            if plan.status == "paused":
                if step.job_id:
                    existing = scheduler.get_job(step.job_id)
                    if existing:
                        logger.info(
                            "Removing job %s for paused plan %s step %s",
                            step.job_id,
                            plan.id,
                            step.id,
                        )
                        remove_job(step.job_id)
                    else:
                        logger.info(
                            "Plan %s step %s paused with missing job %s",
                            plan.id,
                            step.id,
                            step.job_id,
                        )
                    step.job_id = None
                    dirty = True
                logger.info(
                    "Skipping plan %s step %s because plan is paused",
                    plan.id,
                    step.id,
                )
                continue

            if step.status != "approved":
                if step.job_id:
                    existing = scheduler.get_job(step.job_id)
                    if existing:
                        logger.info(
                            "Removing job %s for plan %s step %s due to status %s",
                            step.job_id,
                            plan.id,
                            step.id,
                            step.status,
                        )
                        remove_job(step.job_id)
                    else:
                        logger.info(
                            "Clearing stale job %s for plan %s step %s with status %s",
                            step.job_id,
                            plan.id,
                            step.id,
                            step.status,
                        )
                    step.job_id = None
                    dirty = True
                logger.info(
                    "Skipping scheduling for plan %s step %s with status %s",
                    plan.id,
                    step.id,
                    step.status,
                )
                continue

            if not step.job_id:
                step.job_id = AIPlanStep.generate_job_id(user.id, plan.id)
                dirty = True
                logger.info(
                    "Generated job id %s for plan %s step %s",
                    step.job_id,
                    plan.id,
                    step.id,
                )

            existing_job = scheduler.get_job(step.job_id)
            if not existing_job:
                logger.info(
                    "Plan %s step %s job %s missing in scheduler, re-adding",
                    plan.id,
                    step.id,
                    step.job_id,
                )

            run_date = step.scheduled_for.astimezone(pytz.UTC)
            if run_date <= now_utc:
                run_date = now_utc + timedelta(minutes=1)
                logger.info(
                    "Adjusted run_date for step %s to %s due to past schedule",
                    step.id,
                    run_date.isoformat(),
                )

            # send_scheduled_message — вже топ-рівневий, серіалізується ок
            logger.info(
                "Restoring plan step job %s for user %s (plan %s) at %s",
                step.job_id,
                user.id,
                plan.id,
                run_date.isoformat(),
            )
            scheduler.add_job(
                "app.scheduler:send_scheduled_message",
                "date",
                id=step.job_id,
                run_date=run_date,
                args=[user.tg_id, step.message],
                replace_existing=True,
                misfire_grace_time=3600,
                coalesce=True,
                max_instances=1,
            )

        if dirty:
            db.commit()

    # Тримаємо процес живим
    await asyncio.Event().wait()
