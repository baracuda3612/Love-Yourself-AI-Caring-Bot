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
from app.db import AIPlan, AIPlanDay, AIPlanStep, SessionLocal, User

# Configure JobStore
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


def _generate_step_job_id(user_id: int, step: AIPlanStep) -> str:
    """Generate a unique Job ID based on the new hierarchy."""
    # Safety check if step is detached
    plan_id = "unknown"
    if step.day and step.day.plan_id:
        plan_id = step.day.plan_id
    
    return f"plan_{plan_id}_day_{step.day_id}_step_{step.id}"


def init_scheduler():
    if not scheduler.running:
        scheduler.start()


def shutdown_scheduler():
    scheduler.shutdown(wait=True)


# ——— Utilities ———

def remove_job(job_id):
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass


def _submit_coroutine(coro):
    if _event_loop is None:
        return None
    return asyncio.run_coroutine_threadsafe(coro, _event_loop)


async def _send_message_async(chat_id: int, text: str):
    """Async wrapper to send Telegram message."""
    from app.telegram import bot as tg_bot
    try:
        return await tg_bot.send_message(chat_id, text)
    except Exception as e:
        logger.error(f"Failed to send scheduled message to {chat_id}: {e}")
        return None


def send_scheduled_message(chat_id: int, text: str, step_id: int | None = None):
    """
    Callback function executed by APScheduler.
    """
    _submit_coroutine(_send_message_async(chat_id, text))

    if step_id is not None:
        with SessionLocal() as db:
            step = db.query(AIPlanStep).filter(AIPlanStep.id == step_id).first()
            if step and not step.is_completed:
                # 1. Mark step as completed (trigger logic)
                step.is_completed = True
                step.completed_at = datetime.now(pytz.UTC)
                step.job_id = None
                
                # 2. Check for Plan Completion (Hierarchy Traversal)
                # step -> day -> plan
                if step.day and step.day.plan:
                    plan = step.day.plan
                    
                    # Check if ALL steps in ALL days are completed
                    # (This is an MVP check; optimization possible later)
                    all_done = True
                    for day in plan.days:
                        for s in day.steps:
                            if not s.is_completed:
                                all_done = False
                                break
                        if not all_done:
                            break
                    
                    if all_done:
                        plan.status = "completed"
                        # plan.completed_at = datetime.now(pytz.UTC) # Add column later if needed
                        
                db.commit()


def schedule_plan_step(step: AIPlanStep, user: User) -> bool:
    """
    Schedules a single step. Returns True if a NEW job was created.
    """
    if step.is_completed or step.skipped:
        return False
    
    # We only schedule if we have a concrete time
    if not step.scheduled_for:
        return False
        
    if not user or not user.is_active:
        return False

    new_job_id_assigned = False
    if not step.job_id:
        step.job_id = _generate_step_job_id(user.id, step)
        new_job_id_assigned = True

    # Ensure run_date is in the future
    run_date = step.scheduled_for.astimezone(pytz.UTC)
    now_utc = datetime.now(pytz.UTC)
    if run_date <= now_utc:
        # If passed, schedule for 1 minute from now (catch-up)
        run_date = now_utc + timedelta(minutes=1)

    # Use replace_existing=True to avoid conflicts
    scheduler.add_job(
        "app.scheduler:send_scheduled_message",
        "date",
        id=step.job_id,
        run_date=run_date,
        args=[user.tg_id, f"🔔 {step.title}\n\n{step.description}", step.id],
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )

    return new_job_id_assigned


async def schedule_daily_loop():
    """
    Restores jobs on startup.
    Handles the 3-level hierarchy join: Step -> Day -> Plan.
    """
    global _scheduler_started, _event_loop

    if _scheduler_started:
        return

    _scheduler_started = True
    _event_loop = asyncio.get_running_loop()

    logger.info("Starting scheduler restoration loop...")
    init_scheduler()

    with SessionLocal() as db:
        now_utc = datetime.now(pytz.UTC)
        
        # JOIN: Step -> Day -> Plan -> User
        # Filter: Active User + Active Plan + Future Step + Not Completed
        pending_steps = (
            db.query(AIPlanStep, AIPlanDay, AIPlan, User)
            .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
            .join(AIPlan, AIPlan.id == AIPlanDay.plan_id)
            .join(User, User.id == AIPlan.user_id)
            .filter(
                AIPlan.status == "active",
                User.is_active == True,
                AIPlanStep.is_completed == False,
                AIPlanStep.scheduled_for != None, # Only schedule if time is set
                AIPlanStep.scheduled_for > now_utc
            )
            .all()
        )

        count = 0
        for step, day, plan, user in pending_steps:
            assigned = schedule_plan_step(step, user)
            if assigned:
                count += 1
        
        if count > 0:
            db.commit()
            logger.info(f"Restored {count} scheduled plan steps.")

    # Keep the loop alive
    await asyncio.Event().wait()
