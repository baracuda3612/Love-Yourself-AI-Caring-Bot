# app/scheduler.py
import asyncio
import logging
from math import ceil
from datetime import datetime, timedelta
from typing import Optional

import pytz
from sqlalchemy import func
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app.plan_completion.tokens import make_report_token
from app.db import AIPlan, AIPlanDay, AIPlanStep, SessionLocal, User, UserEvent
from app.ai import async_client
from app.telemetry import log_user_event
from app.ux.catalog import get_trigger_message
from app.ux.persona import get_persona
from app.ux.pulse_prompt import generate_pulse_message
from app.ux.rate_limit import can_send_auto_message
from app.ux.task_notification import format_task_notification, maybe_advance_current_day

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

ADAPTATION_SOFT_TIMEOUT_MIN = 30
ADAPTATION_HARD_TIMEOUT_MIN = 60


def _to_utc(dt: datetime) -> datetime:
    """Safely convert datetime to UTC-aware, handling both naive and aware inputs."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=pytz.UTC)
    return dt.astimezone(pytz.UTC)


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
    scheduler.add_job("app.scheduler:check_plan_completions", "cron", hour=10, minute=30, id="plan_completion_check", replace_existing=True, max_instances=1)
    scheduler.add_job("app.scheduler:send_plan_pulse_snapshots", "cron", hour=10, minute=0, id="pulse_snapshot_check", replace_existing=True, max_instances=1)
    scheduler.add_job(
        "app.scheduler:check_stuck_adaptations",
        "interval",
        minutes=5,
        id="stuck_adaptation_check",
        replace_existing=True,
        max_instances=1,
    )


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
        plan_id = plan.id
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
                day_number = base_context.get("day_number")
                if day_number is not None:
                    maybe_advance_current_day(db, plan_id, day_number)
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
            if delivery_error is None:
                _maybe_schedule_plan_completion(user_id, plan_id)
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

            sent_today_rows = (
                db.query(UserEvent.user_id)
                .filter(
                    UserEvent.event_type == "pulse_sent",
                    func.date(UserEvent.timestamp) == today_utc,
                )
                .all()
            )
            sent_today_ids = {user_id for (user_id,) in sent_today_rows}

            pending_ids: list[int] = []
            for user in active_users:
                if not user.profile:
                    continue
                if user.id not in sent_today_ids:
                    pending_ids.append(user.id)

        async def _send_one(user_id: int):
            async with semaphore:
                try:
                    # Phase 1: read profile
                    with SessionLocal() as db:
                        user = db.query(User).filter(User.id == user_id).first()
                        if not user or not user.profile:
                            return
                        tg_id = user.tg_id
                        persona = user.profile.coach_persona
                        sent_indices = list(user.profile.pulse_sent_indices or [])

                    # Lightweight proxy — no ORM, no session
                    class _ProfileProxy:
                        coach_persona = persona
                        pulse_sent_indices = sent_indices

                    # Phase 2: LLM call — outside DB session
                    text, new_indices = await generate_pulse_message(_ProfileProxy(), async_client)
                    if not text:
                        return

                    # Phase 3: Telegram delivery
                    result = await _send_message_async(tg_id, text)
                    if not result:
                        logger.warning("[PULSE] Delivery failed for user_id=%s", user_id)
                        return

                    # Phase 4: write log + update indices
                    with SessionLocal() as db:
                        user = db.query(User).filter(User.id == user_id).first()
                        if not user or not user.profile:
                            return
                        user.profile.pulse_sent_indices = new_indices
                        log_user_event(
                            db,
                            user_id=user.id,
                            event_type="pulse_sent",
                            context={"persona": persona or "empath"},
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
                # NOTE:
                # task_ignored is system-generated by check_ignored_tasks()
                # and must NOT count as user activity for silence detection.
                # Only user-initiated activity counts as engagement.
                last_event = db.query(UserEvent).filter(
                    UserEvent.user_id == user.id,
                    UserEvent.event_type.in_(["task_completed", "task_skipped", "user_message"]),
                ).order_by(UserEvent.timestamp.desc()).first()

                if not last_event:
                    continue

                days_silent = (now - _to_utc(last_event.timestamp)).days
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
            plan_step_id = event.step_id
            if not plan_step_id:
                continue

            reacted = db.query(UserEvent).filter(
                UserEvent.user_id == event.user_id,
                UserEvent.step_id == str(plan_step_id),
                UserEvent.event_type.in_(["task_completed", "task_skipped"]),
                UserEvent.timestamp >= event.timestamp,
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



def _maybe_schedule_plan_completion(user_id: int, plan_id: int) -> None:
    """
    Called after every task delivery.
    If this was the last scheduled step, queues completion message
    respecting user's local time (no messages after 21:00 local).
    Cron job at 10:30 UTC is the fallback.
    """
    import pytz

    with SessionLocal() as db:
        future_steps = (
            db.query(AIPlanStep)
            .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
            .filter(
                AIPlanDay.plan_id == plan_id,
                AIPlanStep.scheduled_for > datetime.now(pytz.UTC),
                AIPlanStep.canceled_by_adaptation == False,
            )
            .count()
        )
        if future_steps > 0:
            return

        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return

        user_tz_str = getattr(user, "timezone", None) or "Europe/Kyiv"
        try:
            user_tz = pytz.timezone(user_tz_str)
        except Exception:
            user_tz = pytz.timezone("Europe/Kyiv")

        now_local = datetime.now(pytz.UTC).astimezone(user_tz)
        candidate_run_date = datetime.now(pytz.UTC) + timedelta(hours=2)
        candidate_local = candidate_run_date.astimezone(user_tz)

        if now_local.hour >= 21 or candidate_local.hour >= 21:
            next_day = (now_local + timedelta(days=1)).replace(
                hour=10, minute=0, second=0, microsecond=0
            )
            run_date = next_day.astimezone(pytz.UTC)
        else:
            run_date = candidate_run_date

    scheduler.add_job(
        "app.orchestrator:_trigger_plan_completion",
        "date",
        run_date=run_date,
        args=[user_id, plan_id],
        id=f"completion_{plan_id}",
        replace_existing=True,
    )
    logger.info(
        "[SCHEDULER] Last step delivered plan=%s, completion at %s (local=%s)",
        plan_id,
        run_date,
        now_local.strftime("%H:%M"),
    )


async def _send_adaptation_timeout_prompt(user: User, bot) -> None:
    from app.session_memory import session_memory

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="✅ Так, повернутись до плану",
                callback_data="adaptation_timeout_reset",
            ),
            InlineKeyboardButton(
                text="🔄 Ні, продовжую",
                callback_data="adaptation_timeout_continue",
            ),
        ]]
    )
    text = "Схоже, налаштування плану зупинилось. Повернутись до виконання плану?"

    try:
        await bot.send_message(user.tg_id, text, reply_markup=keyboard)
        await session_memory.set_adaptation_soft_prompted(user.id)
    except Exception:
        logger.warning("[ADAPT_TIMEOUT] Failed to send prompt user=%s", user.id)


async def _force_reset_adaptation(user: User, db) -> None:
    from app.session_memory import session_memory

    user.current_state = "ACTIVE"
    db.add(user)
    db.commit()
    await session_memory.clear_adaptation_context(user.id)
    await session_memory.clear_adaptation_last_active(user.id)
    await session_memory.clear_adaptation_soft_prompted(user.id)
    logger.info("[ADAPT_TIMEOUT] Hard reset user=%s", user.id)


def check_stuck_adaptations() -> None:
    if not _event_loop:
        return

    async def _run():
        from app.fsm.states import ADAPTATION_FLOW_STATES
        from app.session_memory import session_memory
        from app.telegram import bot as tg_bot

        with SessionLocal() as db:
            stuck_users = (
                db.query(User)
                .filter(User.current_state.in_(list(ADAPTATION_FLOW_STATES)))
                .all()
            )

            now = datetime.utcnow()

            for user in stuck_users:
                if not user.tg_id:
                    continue

                last_active = await session_memory.get_adaptation_last_active(user.id)
                if last_active is None:
                    await session_memory.set_adaptation_last_active(user.id)
                    continue

                minutes_idle = (now - last_active).total_seconds() / 60
                already_prompted = await session_memory.get_adaptation_soft_prompted(user.id)

                if minutes_idle >= ADAPTATION_HARD_TIMEOUT_MIN and already_prompted:
                    await _force_reset_adaptation(user, db)
                elif minutes_idle >= ADAPTATION_SOFT_TIMEOUT_MIN and not already_prompted:
                    await _send_adaptation_timeout_prompt(user, tg_bot)

    asyncio.run_coroutine_threadsafe(_run(), _event_loop)


def check_plan_completions() -> None:
    """
    Daily cron at 10:30 UTC.
    Safety net: finds users with active plans past end_date
    and triggers completion. Primary trigger is _maybe_schedule_plan_completion.
    This catches users who stopped opening the bot.
    """
    from app.orchestrator import _auto_complete_plan_if_needed, send_plan_completion_message

    now = datetime.now(pytz.UTC)
    completed_pairs: list[tuple[int, int]] = []

    with SessionLocal() as db:
        expired_users = (
            db.query(User)
            .join(AIPlan, AIPlan.user_id == User.id)
            .filter(
                AIPlan.status == "active",
                User.plan_end_date < now,
                User.plan_end_date.isnot(None),
            )
            .all()
        )
        for user in expired_users:
            candidate_plan = (
                db.query(AIPlan)
                .filter(AIPlan.user_id == user.id, AIPlan.status == "active")
                .order_by(AIPlan.created_at.desc())
                .first()
            )
            try:
                _auto_complete_plan_if_needed(db, user)
                if candidate_plan and candidate_plan.status == "completed":
                    completed_pairs.append((user.id, candidate_plan.id))
            except Exception as e:
                logger.error("[CRON_COMPLETION] failed user=%s: %s", user.id, e)

        try:
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error("[CRON_COMPLETION] commit failed: %s", e)
            return

    if not _event_loop:
        return

    for user_id, plan_id in completed_pairs:
        future = _submit_coroutine(send_plan_completion_message(user_id, plan_id))
        if not future:
            continue
        try:
            future.result(timeout=30)
        except Exception as e:
            logger.error(
                "[CRON_COMPLETION] send failed user=%s plan=%s: %s",
                user_id,
                plan_id,
                e,
            )


PULSE_THRESHOLDS = {
    "MEDIUM": [7],
    "STANDARD": [7, 14],
    "LONG": [14, 28, 42, 56, 70, 84],
}


def _now_in_user_tz(user: User) -> datetime:
    tz_name = getattr(user, "timezone", None) or "Europe/Kyiv"
    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = pytz.timezone("Europe/Kyiv")
    return datetime.now(pytz.UTC).astimezone(tz)


async def check_pulse_triggers(db, bot) -> None:
    active_plans = db.query(AIPlan).filter(AIPlan.status == "active").all()

    for plan in active_plans:
        thresholds = PULSE_THRESHOLDS.get(plan.duration, [])
        if not thresholds:
            continue

        user = db.query(User).filter(User.id == plan.user_id).first()
        if not user or not user.tg_id:
            continue
        if not can_deliver_tasks(user):
            continue

        today = _now_in_user_tz(user).date()
        active_day = getattr(plan, "current_day", None) or 1
        active_day = min(active_day, getattr(plan, "total_days", None) or active_day)

        if active_day not in thresholds:
            continue

        already_sent = (
            db.query(UserEvent)
            .filter(
                UserEvent.user_id == plan.user_id,
                UserEvent.event_type == "pulse_sent",
                UserEvent.context["plan_id"].astext == str(plan.id),
                UserEvent.context["active_day"].astext == str(active_day),
            )
            .first()
        )
        if already_sent:
            continue

        token = make_report_token(plan.id, settings.REPORT_TOKEN_SECRET)
        url = f"{settings.APP_BASE_URL}/pulse/{token}"
        week_num = ceil(active_day / 7)

        text = (
            f"📊 Тиждень {week_num} — snapshot твого плану.\n\n"
            f"Подивись як іде процес:"
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Відкрити", url=url)]]
        )

        try:
            await bot.send_message(user.tg_id, text, reply_markup=keyboard)
        except Exception:
            continue

        log_user_event(
            db,
            user_id=plan.user_id,
            event_type="pulse_sent",
            context={"active_day": active_day, "plan_id": plan.id, "date": str(today)},
        )
        db.commit()


def send_plan_pulse_snapshots() -> None:
    if not _event_loop:
        logger.warning("[PULSE_SNAPSHOT] No event loop available")
        return

    async def _run():
        from app.telegram import bot as tg_bot

        with SessionLocal() as db:
            await check_pulse_triggers(db, tg_bot)

    asyncio.run_coroutine_threadsafe(_run(), _event_loop)
