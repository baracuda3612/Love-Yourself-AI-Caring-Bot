# app/scheduler.py
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

import pytz
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler

from app.config import settings
from app.db import AIPlan, AIPlanDay, AIPlanStep, SessionLocal, User
from app.telemetry import log_user_event

# Configure JobStore
DATABASE_URL = settings.DATABASE_URL
jobstore_url = DATABASE_URL

jobstores = {"default": SQLAlchemyJobStore(url=jobstore_url)}
scheduler = BackgroundScheduler(jobstores=jobstores, timezone="UTC")

logger = logging.getLogger(__name__)

_scheduler_started = False
_event_loop: Optional[asyncio.AbstractEventLoop] = None
_ALLOWED_USER_STATES = {"ACTIVE", "ADAPTATION_FLOW", "ACTIVE_CONFIRMATION"}

_DELIVERY_LATE_GRACE = timedelta(minutes=1)


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


def send_scheduled_message(_chat_id: int, text: str, step_id: int | None = None):
    """
    Callback function executed by APScheduler.
    """
    if step_id is None:
        return

    with SessionLocal() as db:
        step = db.query(AIPlanStep).filter(AIPlanStep.id == step_id).first()
        if not step or not step.day or not step.day.plan:
            return

        plan = step.day.plan
        user = plan.user
        if not user or not user.is_active:
            return
        if user.current_state not in _ALLOWED_USER_STATES:
            return

        if plan.status != "active":
            return
        if step.is_completed or step.skipped:
            return
        if not step.scheduled_for:
            return

        scheduled_for = step.scheduled_for.astimezone(pytz.UTC)
        now_utc = datetime.now(pytz.UTC)
        if now_utc < scheduled_for:
            return
        if now_utc - scheduled_for > _DELIVERY_LATE_GRACE:
            return

        content_id = (
            getattr(step, "content_id", None)
            or getattr(step, "content_library_id", None)
        )
        plan_step_id = step.id
        user_id = user.id
        send_chat_id = user.tg_id

    future = _submit_coroutine(_send_message_async(send_chat_id, text))
    if not future:
        return

    delivery_error = None
    try:
        result = future.result(timeout=10)
        if result is None:
            delivery_error = "send_failed"
    except Exception as exc:
        delivery_error = str(exc)

    with SessionLocal() as db:
        try:
            base_context = {}
            if not content_id:
                base_context = {
                    "plan_step_title": step.title,
                    "plan_step_description": step.description,
                }
            if delivery_error is None:
                log_user_event(
                    db,
                    user_id,
                    "task_delivered",
                    content_id=content_id,
                    plan_step_id=plan_step_id,
                    context=base_context,
                )
            else:
                error_context = {**base_context, "error": delivery_error}
                log_user_event(
                    db,
                    user_id,
                    "task_delivery_failed",
                    content_id=content_id,
                    plan_step_id=plan_step_id,
                    context=error_context,
                )
            db.commit()
        except Exception:
            logger.exception("Failed to log scheduler telemetry.")


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
    if user.current_state not in _ALLOWED_USER_STATES:
        return False

    if not step.day or not step.day.plan:
        return False
    if step.day.plan.status != "active":
        return False

    job_id = getattr(step, "job_id", None) or _generate_step_job_id(user.id, step)
    new_job_id_assigned = getattr(step, "job_id", None) is None

    # Ensure run_date is in the future
    run_date = step.scheduled_for.astimezone(pytz.UTC)
    now_utc = datetime.now(pytz.UTC)
    if run_date <= now_utc:
        return False

    # Use replace_existing=True to avoid conflicts
    scheduler.add_job(
        "app.scheduler:send_scheduled_message",
        "date",
        id=job_id,
        run_date=run_date,
        args=[user.tg_id, f"🔔 {step.title}\n\n{step.description}", step.id],
        replace_existing=True,
        misfire_grace_time=1,
        coalesce=False,
        max_instances=1,
    )

    return new_job_id_assigned


def cancel_plan_step_jobs(step_ids: list[int]) -> int:
    if not step_ids:
        return 0
    removed = 0
    with SessionLocal() as db:
        steps = (
            db.query(AIPlanStep)
            .filter(AIPlanStep.id.in_(step_ids))
            .all()
        )
        for step in steps:
            plan_user_id = step.day.plan.user_id if step.day and step.day.plan else 0
            job_id = getattr(step, "job_id", None) or _generate_step_job_id(plan_user_id, step)
            try:
                scheduler.remove_job(job_id)
            except Exception:
                continue
            else:
                removed += 1
    return removed


def reschedule_plan_steps(step_ids: list[int]) -> int:
    if not step_ids:
        return 0
    created = 0
    with SessionLocal() as db:
        steps = (
            db.query(AIPlanStep, AIPlanDay, AIPlan, User)
            .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
            .join(AIPlan, AIPlan.id == AIPlanDay.plan_id)
            .join(User, User.id == AIPlan.user_id)
            .filter(AIPlanStep.id.in_(step_ids))
            .all()
        )
        for step, _, plan, user in steps:
            if plan.status != "active":
                continue
            if schedule_plan_step(step, user):
                created += 1
        if created > 0:
            db.commit()
    return created


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
                User.current_state.in_(_ALLOWED_USER_STATES),
                AIPlanStep.is_completed == False,
                AIPlanStep.skipped == False,
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
