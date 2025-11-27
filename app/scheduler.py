# app/scheduler.py
import asyncio
import logging
import os
from datetime import datetime, timedelta, time as dt_time
from typing import Optional

import pytz
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.db import AIPlan, AIPlanStep, SessionLocal, User

# jobstore: окремий sqlite файл поруч із основною БД, або той самий Postgres
DATABASE_URL = settings.DATABASE_URL

if DATABASE_URL.startswith("sqlite:///"):
    base_path = DATABASE_URL.replace("sqlite:///", "")
    base_dir = os.path.dirname(os.path.abspath(base_path)) or "."
    jobs_db_path = os.path.join(base_dir, "jobs.sqlite")
    jobstore_url = f"sqlite:///{jobs_db_path}"
else:
    jobstore_url = DATABASE_URL

jobstores = {"default": SQLAlchemyJobStore(url=jobstore_url)}
scheduler = BackgroundScheduler(jobstores=jobstores, timezone="UTC")

logger = logging.getLogger(__name__)

_scheduler_started = False
_event_loop: Optional[asyncio.AbstractEventLoop] = None


def _generate_step_job_id(user: User, step: AIPlanStep) -> str:
    """Генерує job_id, сумісний із попередньою логікою."""
    if hasattr(AIPlanStep, "generate_job_id"):
        return AIPlanStep.generate_job_id(user.id, step.plan_id)
    return f"plan_step_{user.id}_{step.plan_id or 'plan'}_{step.id or 'step'}"


def init_scheduler():
    scheduler.start()


def shutdown_scheduler():
    scheduler.shutdown(wait=True)


# ——— утиліти ———

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
    notification_time: dt_time | None = user.notification_time
    hour = notification_time.hour if notification_time else settings.DEFAULT_SEND_HOUR
    minute = notification_time.minute if notification_time else 0
    return CronTrigger(hour=hour, minute=minute, timezone=pytz.timezone(tz))


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
    Топ-рівневий callable (серіалізується як 'module:function').
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
    """Щоденне повідомлення (тимчасово вимкнено до Plan Agent)."""
    logger.info("Daily message skipped (Agent WIP) for user %s", user_id)


# ——— топ-рівневі callable для APScheduler (жодних замикань/лямбд) ———

def run_daily_job(user_id: int):
    """Sync wrapper — викликається з jobstore; запускає корутину у головному loop."""
    _submit_coroutine(_send_daily(user_id))


# ——— API реєстрації задач ———

def schedule_daily_delivery(user: User):
    """Щоденне повідомлення за notification_time / timezone (тимчасово вимкнено)."""
    if not user.is_active:
        return

    logger.info("Daily delivery scheduling skipped (Agent WIP) for user %s", user.id)


def schedule_plan_step(step: AIPlanStep, user: User) -> bool:
    """
    Додає job для кроку плану.
    Повертає True, якщо щойно видали новий job_id (було None).
    """
    if step.is_completed:
        return False
    # лише approved кроки шедулимо
    if step.status and step.status != "approved":
        return False
    if not user or not user.is_active:
        return False
    if not step.scheduled_for:
        return False

    new_job_id_assigned = False
    if not step.job_id:
        step.job_id = _generate_step_job_id(user, step)
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
    - кроки планів зі статусом approved (і з проставленим scheduled_for),
      ігноруючи paused/pending/canceled.
    """
    # Тимчасово повністю вимикаємо відновлення задач
    return

    global _scheduler_started, _event_loop

    if _scheduler_started:
        return

    _scheduler_started = True
    _event_loop = asyncio.get_running_loop()

    logger.info("Starting scheduler restoration loop")
    init_scheduler()

    with SessionLocal() as db:
        # 1) Daily — тимчасово вимкнено
        users = db.query(User).filter(User.is_active == True).all()
        for user in users:
            schedule_daily_delivery(user)

        # 2) Планові кроки (approved + майбутні + не completed) для активних юзерів і не paused планів
        now_utc = datetime.now(pytz.UTC)
        plan_rows = (
            db.query(AIPlanStep, AIPlan, User)
            .join(AIPlan, AIPlan.id == AIPlanStep.plan_id)
            .join(User, User.id == AIPlan.user_id)
            .filter(
                AIPlan.status.in_(["active", "draft"]),  # draft не шедулимо, але нормалізуємо статуси
                AIPlanStep.is_completed == False,
                AIPlanStep.scheduled_for != None,
                AIPlanStep.scheduled_for > now_utc,
                User.is_active == True,
            )
            .all()
        )

        dirty = False
        for step, plan, user in plan_rows:
            # нормалізація статусу: None -> pending (щоб не шедулити)
            if step.status is None:
                step.status = "pending"
                dirty = True

            # пропускаємо paused плани — і чистимо застарілі job-и
            if plan.status == "paused":
                if step.job_id:
                    existing = scheduler.get_job(step.job_id)
                    if existing:
                        logger.info(
                            "Removing job %s for paused plan %s step %s",
                            step.job_id, plan.id, step.id,
                        )
                        remove_job(step.job_id)
                    else:
                        logger.info(
                            "Plan %s step %s paused with missing job %s",
                            plan.id, step.id, step.job_id,
                        )
                    step.job_id = None
                    dirty = True
                logger.info("Skipping plan %s step %s because plan is paused", plan.id, step.id)
                continue

            # шедулимо тільки approved
            if step.status != "approved":
                if step.job_id:
                    existing = scheduler.get_job(step.job_id)
                    if existing:
                        logger.info(
                            "Removing job %s for plan %s step %s due to status %s",
                            step.job_id, plan.id, step.id, step.status,
                        )
                        remove_job(step.job_id)
                    else:
                        logger.info(
                            "Clearing stale job %s for plan %s step %s with status %s",
                            step.job_id, plan.id, step.id, step.status,
                        )
                    step.job_id = None
                    dirty = True
                logger.info(
                    "Skipping scheduling for plan %s step %s with status %s",
                    plan.id, step.id, step.status,
                )
                continue

            # якщо job_id відсутній — згенерувати
            if not step.job_id:
                step.job_id = _generate_step_job_id(user, step)
                dirty = True
                logger.info(
                    "Generated job id %s for plan %s step %s",
                    step.job_id, plan.id, step.id,
                )

            existing_job = scheduler.get_job(step.job_id)
            if not existing_job:
                logger.info(
                    "Plan %s step %s job %s missing in scheduler, re-adding",
                    plan.id, step.id, step.job_id,
                )

            # віддати на універсальний шедулер (він ще раз перевірить дані)
            assigned = schedule_plan_step(step, user)
            if assigned:
                dirty = True

        if dirty:
            db.commit()

    # Тримаємо процес живим
    await asyncio.Event().wait()
