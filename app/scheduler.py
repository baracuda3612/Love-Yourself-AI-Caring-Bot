# app/scheduler.py
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

import pytz
from sqlalchemy import func
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app.db import AIPlan, AIPlanDay, AIPlanStep, SessionLocal, User, UserEvent
from app.ai import async_client
from app.telemetry import log_user_event
from app.ux.catalog import get_trigger_message
from app.ux.persona import get_persona
from app.ux.pulse_prompt import generate_pulse_message
from app.ux.rate_limit import can_send_auto_message
from app.ux.task_notification import format_task_notification

# Configure JobStore
DATABASE_URL = settings.DATABASE_URL
jobstore_url = DATABASE_URL

jobstores = {"default": SQLAlchemyJobStore(url=jobstore_url)}
scheduler = BackgroundScheduler(jobstores=jobstores, timezone="UTC")

logger = logging.getLogger(__name__)

# TECH-DEBT TD-3:
# Never reuse ORM objects across SQLAlchemy sessions.
# Always re-fetch entities inside the session where they are used.

_scheduler_started = False
_event_loop: Optional[asyncio.AbstractEventLoop] = None
_ALLOWED_USER_STATES = {"ACTIVE"}

_DELIVERY_LATE_GRACE = timedelta(minutes=1)


def _generate_step_job_id(step: AIPlanStep) -> str:
    """Generate a deterministic Job ID from persistent plan/day/step identifiers."""
    plan_id = step.day.plan_id if step.day and step.day.plan_id is not None else "unknown"
    return f"plan_{plan_id}_day_{step.day_id}_step_{step.id}"


def init_scheduler():
    if not scheduler.running:
        scheduler.start()
    scheduler.add_job("app.scheduler:send_daily_pulse", "cron", hour=9, minute=0, id="daily_pulse", replace_existing=True, max_instances=1)
    scheduler.add_job("app.scheduler:check_silent_users", "cron", hour=12, minute=0, id="silent_check", replace_existing=True, max_instances=1)
    scheduler.add_job("app.scheduler:check_ignored_tasks", "cron", hour=8, minute=0, id="ignored_check", replace_existing=True, max_instances=1)


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


def can_deliver_tasks(user: User) -> bool:
    # NOTE:
    # Missed scheduled deliveries while user is not ACTIVE are intentional.
    # Replay/recovery (if any) must be handled by adaptation/reconciliation layer.
    return user.current_state == "ACTIVE"


async def _send_message_async(
    chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    """Async wrapper to send Telegram message."""
    from app.telegram import bot as tg_bot
    try:
        return await tg_bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=reply_markup)
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
        if not can_deliver_tasks(user):
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

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Виконано",
                    callback_data=f"task_complete:{plan_step_id}",
                ),
                InlineKeyboardButton(
                    text="⏭️ Пропустити",
                    callback_data=f"task_skip:{plan_step_id}",
                ),
            ]
        ]
    )

    future = _submit_coroutine(_send_message_async(send_chat_id, text, reply_markup=keyboard))
    if not future:
        return

    delivery_error = None
    try:
        result = future.result(timeout=30)
        if result is None:
            delivery_error = "send_failed"
    except Exception as exc:
        delivery_error = str(exc)

    with SessionLocal() as db:
        try:
            base_context = {
                "exercise_id": step.exercise_id,
                "day_number": step.day.day_number if step.day else None,
            }
            if not content_id:
                base_context.update(
                    {
                        "plan_step_title": step.title,
                        "plan_step_description": step.description,
                    }
                )
            if delivery_error is None:
                log_user_event(
                    db,
                    user_id=user_id,
                    event_type="task_delivered",
                    plan_step_id=plan_step_id,
                    context=base_context,
                )
            else:
                error_context = {**base_context, "error": delivery_error}
                log_user_event(
                    db,
                    user_id=user_id,
                    event_type="task_delivery_failed",
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
    if not can_deliver_tasks(user):
        return False

    if not step.day or not step.day.plan:
        return False
    if step.day.plan.status != "active":
        return False

    job_id = getattr(step, "job_id", None) or _generate_step_job_id(step)
    new_job_id_assigned = getattr(step, "job_id", None) is None

    # Ensure run_date is in the future
    run_date = step.scheduled_for.astimezone(pytz.UTC)
    now_utc = datetime.now(pytz.UTC)
    if run_date <= now_utc:
        return False

    logger.info("Scheduling job %s (replace_existing=True)", job_id)

    # Use replace_existing=True to avoid conflicts
    with SessionLocal() as _db:
        db_step = _db.query(AIPlanStep).filter(AIPlanStep.id == step.id).first()
        all_today = (
            _db.query(AIPlanStep)
            .filter(AIPlanStep.day_id == step.day_id, AIPlanStep.canceled_by_adaptation == False)
            .order_by(AIPlanStep.order_in_day)
            .all()
        )
        task_total = len(all_today)
        task_index = next((i + 1 for i, s in enumerate(all_today) if s.id == step.id), 1)

        # TECH-DEBT TD-4:
        # Task notification must be formatted at delivery time.
        # Adaptation layer can mutate step content after scheduling.
        # TODO: notification_text is baked at schedule time, not delivery time.
        # If adaptation changes step title/content between scheduling and delivery,
        # user will see stale text. Fix when adaptation layer is stable:
        # move format_task_notification() into send_scheduled_message() where
        # step is re-fetched from DB fresh at delivery time.
        notification_text = format_task_notification(
            db=_db,
            step=db_step or step,
            day=(db_step.day if db_step else step.day),
            plan_day_number=(db_step.day.day_number if db_step and db_step.day else step.day.day_number),
            task_index=task_index,
            task_total=task_total,
        )

    scheduler.add_job(
        "app.scheduler:send_scheduled_message",
        "date",
        id=job_id,
        run_date=run_date,
        args=[user.tg_id, notification_text, step.id],
        replace_existing=True,
        misfire_grace_time=None,
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
            job_id = getattr(step, "job_id", None) or _generate_step_job_id(step)
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
                User.current_state == "ACTIVE",
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


def send_daily_pulse():
    """
    Runs at 9:00 UTC. Fires pulse for all active users concurrently.
    Fire-and-forget: scheduler does not wait for completion.
    Errors are logged per-user and do not affect other users.
    """
    if not _event_loop:
        logger.warning("[PULSE] No event loop available")
        return

    MAX_CONCURRENT = 20

    async def _run():
        today_utc = datetime.now(pytz.UTC).date()
        semaphore = asyncio.Semaphore(MAX_CONCURRENT)

        with SessionLocal() as db:
            active_users = db.query(User).filter(
                User.is_active == True,
                User.current_state == "ACTIVE",
                User.tg_id.isnot(None),
            ).all()

            # TECH-DEBT TD-5:
            # Current implementation performs N+1 query for pulse_sent detection.
            # Refactor to single query fetching today's pulse_sent user_ids
            # before scaling beyond ~1000 active users.
            pending_ids: list[int] = []
            for user in active_users:
                if not user.profile:
                    continue
                already = db.query(UserEvent).filter(
                    UserEvent.user_id == user.id,
                    UserEvent.event_type == "pulse_sent",
                    func.date(UserEvent.timestamp) == today_utc,
                ).first()
                if not already:
                    pending_ids.append(user.id)

        async def _send_one(user_id: int):
            async with semaphore:
                try:
                    with SessionLocal() as db:
                        user = db.query(User).filter(User.id == user_id).first()
                        if not user or not user.profile:
                            return

                        text = await generate_pulse_message(user.profile, db, async_client)
                        if not text:
                            return

                        result = await _send_message_async(user.tg_id, text)
                        if not result:
                            logger.warning("[PULSE] Delivery failed for user_id=%s, skipping log", user.id)
                            return

                        log_user_event(
                            db,
                            user_id=user.id,
                            event_type="pulse_sent",
                            context={"persona": user.profile.coach_persona or "empath"},
                        )
                        db.commit()
                except Exception:
                    logger.error("[PULSE] user_id=%s", user_id, exc_info=True)

        await asyncio.gather(*[_send_one(uid) for uid in pending_ids])
        logger.info("[PULSE] Completed for %d users", len(pending_ids))

    asyncio.run_coroutine_threadsafe(_run(), _event_loop)


def check_silent_users():
    """Runs at 12:00 UTC. Max 1 re-engagement message per user."""
    with SessionLocal() as db:
        now = datetime.now(pytz.UTC)
        today = now.date()
        active_users = db.query(User).filter(
            User.is_active == True,
            User.current_state == "ACTIVE",
            User.tg_id.isnot(None),
        ).all()

        for user in active_users:
            try:
                last_event = db.query(UserEvent).filter(
                    UserEvent.user_id == user.id,
                    UserEvent.event_type.in_(["task_completed", "task_skipped", "task_ignored", "user_message"]),
                ).order_by(UserEvent.timestamp.desc()).first()

                if not last_event:
                    continue

                days_silent = (now - last_event.timestamp.replace(tzinfo=pytz.UTC)).days
                if days_silent < 2:
                    continue

                sent_today = db.query(UserEvent).filter(
                    UserEvent.user_id == user.id,
                    UserEvent.event_type == "silent_sent",
                    func.date(UserEvent.timestamp) == today,
                ).first()
                if sent_today:
                    continue

                recent_silent = db.query(UserEvent).filter(
                    UserEvent.user_id == user.id,
                    UserEvent.event_type == "silent_sent",
                    UserEvent.timestamp >= now - timedelta(days=6),
                ).first()
                if recent_silent:
                    continue

                if not can_send_auto_message(db, user.id, "silent_sent"):
                    continue

                trigger_id = "silent_5_days" if days_silent >= 5 else "silent_2_days"
                msg = get_trigger_message(trigger_id, get_persona(user.profile), {"name": user.first_name})
                if not msg:
                    continue

                future = _submit_coroutine(_send_message_async(user.tg_id, msg))
                if not future:
                    continue

                try:
                    result = future.result(timeout=30)
                except Exception:
                    logger.warning("[SILENT] Delivery exception user_id=%s", user.id, exc_info=True)
                    continue

                if not result:
                    logger.warning("[SILENT] Delivery returned None for user_id=%s", user.id)
                    continue

                log_user_event(
                    db,
                    user_id=user.id,
                    event_type="silent_sent",
                    context={"trigger": trigger_id, "days_silent": days_silent},
                )
                db.commit()
            except Exception:
                logger.error("[SILENT] user_id=%s", user.id, exc_info=True)


def check_ignored_tasks():
    """
    Runs at 8:00 UTC.
    Finds delivered steps in the last day without reaction and logs task_ignored telemetry only.
    """
    with SessionLocal() as db:
        # TECH-DEBT TD-6:
        # This logic uses sliding 24h window, not calendar-day semantics.
        # Refactor if strict day-based behavior is required.
        yesterday_start = datetime.now(pytz.UTC) - timedelta(days=1)
        yesterday_end = datetime.now(pytz.UTC)

        delivered = db.query(UserEvent).filter(
            UserEvent.event_type == "task_delivered",
            UserEvent.timestamp >= yesterday_start,
            UserEvent.timestamp < yesterday_end,
        ).all()

        for event in delivered:
            plan_step_id = event.plan_step_id
            if not plan_step_id:
                continue

            reacted = db.query(UserEvent).filter(
                UserEvent.user_id == event.user_id,
                UserEvent.step_id == str(plan_step_id),
                UserEvent.event_type.in_(["task_completed", "task_skipped"]),
            ).first()
            if reacted:
                continue

            already_logged = db.query(UserEvent).filter(
                UserEvent.user_id == event.user_id,
                UserEvent.step_id == str(plan_step_id),
                UserEvent.event_type == "task_ignored",
            ).first()
            if already_logged:
                continue

            log_user_event(
                db,
                user_id=event.user_id,
                event_type="task_ignored",
                plan_step_id=plan_step_id,
                context={"detected_at": "morning_check"},
            )
        db.commit()
