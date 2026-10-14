# app/scheduler.py
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import logging
from threading import Lock
from math import ceil
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytz
from sqlalchemy import and_, case, func, or_
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app.active_days import resolve_timezone, step_expires_at
from app.plan_completion.tokens import make_report_token
from app.db import AIPlan, AIPlanDay, AIPlanStep, SessionLocal, User, UserEvent
from app.ai import async_client
from app.telemetry import log_user_event
from app.ux.catalog import get_trigger_message
from app.ux.persona import get_persona
from app.ux.pulse_prompt import generate_pulse_message
from app.ux.rate_limit import can_send_auto_message
from app.exercise_delivery import ExerciseSendResult, send_exercise
from app.lifecycle import (
    LifecycleTransitionError,
    derive_current_day,
    expire_plan_step,
)

# Configure JobStore
DATABASE_URL = settings.DATABASE_URL
jobstore_url = DATABASE_URL

jobstores = {"default": SQLAlchemyJobStore(url=jobstore_url), "delivery_recovery": MemoryJobStore(), "completion_recovery": MemoryJobStore()}
scheduler = BackgroundScheduler(jobstores=jobstores, timezone="UTC")

logger = logging.getLogger(__name__)

# TECH-DEBT TD-3:
# Never reuse ORM objects across SQLAlchemy sessions.
# Always re-fetch entities inside the session where they are used.

_scheduler_started = False
_event_loop: Optional[asyncio.AbstractEventLoop] = None
_recovery_job_lock = Lock()

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
        scheduler.start(paused=True)
    # P1 frozen: keep send_daily_pulse() available, but disable its cron trigger.
    # scheduler.add_job("app.scheduler:send_daily_pulse", "cron", hour=9, minute=0, id="daily_pulse", replace_existing=True, max_instances=1)
    # Remove persisted jobs for retired runtime entrances. APScheduler owns its
    # job table, so cleanup stays at the scheduler boundary rather than Alembic.
    for retired_job_id in (
        "daily_pulse",
        "stuck_schedule_adj_check",
        "ignored_check",
        "scheduled_delivery_recovery",
    ):
        try:
            scheduler.remove_job(retired_job_id)
        except Exception:
            pass
    for job in scheduler.get_jobs():
        if job.id.startswith(("completion_", "completion_retry_", "completion_receipt_retry_")):
            remove_job(job.id)
    scheduler.add_job("app.scheduler:expire_overdue_steps", "interval", seconds=30, id="expire_steps", replace_existing=True, max_instances=1)
    scheduler.add_job("app.scheduler:check_silent_users", "cron", hour=12, minute=0, id="silent_check", replace_existing=True, max_instances=1)
    scheduler.add_job("app.scheduler:check_plan_completions", "cron", hour=10, minute=30, id="plan_completion_check", replace_existing=True, max_instances=1)
    scheduler.add_job("app.scheduler:send_plan_pulse_snapshots", "cron", hour=10, minute=0, id="pulse_snapshot_check", replace_existing=True, max_instances=1)
    scheduler.resume()


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


def _enable_delivery_recovery():
    """Arm a failure-only alarm independent of DB availability; park on success."""
    now = datetime.now(timezone.utc)
    # Serialize the final DB check with wakeups, so a concurrently committed
    # failure cannot lose its recovery job during shutdown of the worker.
    with _recovery_job_lock:
        try:
            run_at = _delivery_recovery_time(now)
        except Exception:
            # Durable work stays in PostgreSQL; the small fault alarm must run
            # even while that database/job store cannot accept a new timer.
            logger.exception('Delivery recovery database temporarily unavailable')
            run_at = now + timedelta(seconds=30)
        if run_at is None:
            try:
                scheduler.remove_job('scheduled_delivery_recovery', jobstore='delivery_recovery')
            except Exception:
                pass
            return
        job = scheduler.get_job('scheduled_delivery_recovery', jobstore='delivery_recovery')
        existing_at = getattr(job, 'next_run_time', None)
        if existing_at is not None and _to_utc(existing_at) <= run_at and run_at <= now + timedelta(seconds=31):
            return
        if job is not None:
            scheduler.modify_job('scheduled_delivery_recovery', jobstore='delivery_recovery', next_run_time=run_at)
        else:
            # Retain the alarm when a run raises; removal is explicit only after
            # DB proof that pending work is repaired, quarantined or terminal.
            scheduler.add_job('app.scheduler:reconcile_scheduled_deliveries', 'interval',
                              seconds=30, start_date=run_at, next_run_time=run_at,
                              id='scheduled_delivery_recovery', jobstore='delivery_recovery',
                              replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=None)


def _delivery_recovery_time(now):
    from app.db import ExerciseDelivery
    from app.exercise_status import pending_status_step_ids
    from app.scheduled_delivery import IN_FLIGHT_GRACE, LATE_GRACE
    with SessionLocal() as db:
        retry_at = db.query(func.min(case(
                (AIPlanStep.step_status != 'pending', now),
                (and_(AIPlan.status == 'active', User.is_active.is_(True)), func.greatest(ExerciseDelivery.next_attempt_at, AIPlanStep.scheduled_for)),
                else_=func.least(AIPlanStep.expires_at, AIPlanStep.scheduled_for + LATE_GRACE),
            ))).select_from(ExerciseDelivery).join(AIPlanStep, AIPlanStep.id == ExerciseDelivery.plan_step_id).join(
                AIPlanDay, AIPlanDay.id == AIPlanStep.day_id).join(AIPlan, AIPlan.id == AIPlanDay.plan_id).join(
                User, User.id == AIPlan.user_id).filter(ExerciseDelivery.state == 'retryable').scalar()
        started_at = db.query(func.min(ExerciseDelivery.started_at)).filter(
            ExerciseDelivery.state == 'in_flight').scalar()
        times = []
        if retry_at is not None:
            times.append(_to_utc(retry_at))
        if started_at is not None:
            times.append(_to_utc(started_at) + IN_FLIGHT_GRACE)
        if pending_status_step_ids(db) or _due_delivery_steps(db, now).first() is not None:
            times.append(now + timedelta(seconds=30))
    return max(now + timedelta(seconds=30), min(times)) if times else None


def can_deliver_tasks(user: User, plan: AIPlan | None = None) -> bool:
    # NOTE:
    # Missed scheduled deliveries while user is not ACTIVE are intentional.
    # Replay/recovery (if any) must be handled by adaptation/reconciliation layer.
    if not user or not user.is_active:
        return False
    if plan is not None:
        return plan.status == "active"
    return any(getattr(candidate, "status", None) == "active" for candidate in user.plans)


def _active_plan_users_query(db):
    """Return enabled Telegram users whose authoritative plan is active."""
    return (
        db.query(User)
        .join(AIPlan, AIPlan.user_id == User.id)
        .filter(
            AIPlan.status == "active",
            User.is_active == True,
            User.tg_id.isnot(None),
        )
    )


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
    """Claim once before Telegram; persist late results inside the send coroutine."""
    if step_id is None or _event_loop is None:
        return
    from app.scheduled_delivery import claim_send, finish_send
    from app.active_days import resolve_active_days, is_active_day
    with SessionLocal() as db:
        step = db.get(AIPlanStep, step_id)
        if step is None:
            return
        user = step.day.plan.user
        user_id, plan_id = user.id, step.day.plan_id
        today = datetime.now(timezone.utc).astimezone(resolve_timezone(user.timezone)).date()
        if not is_active_day(today, resolve_active_days(user.profile)):
            return
        try:
            claim = claim_send(db, step_id)
            db.commit()
        except ValueError:
            db.rollback()
            logger.exception("Invalid scheduled presentation step=%s", step_id)
            return
    if claim is None:
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Виконано", callback_data=f"task_complete:{step_id}"),
        InlineKeyboardButton(text="Пропустити", callback_data=f"task_skip:{step_id}"),
    ]])

    def persist(result):
        with SessionLocal() as db:
            finish_send(db, claim.attempt_id, result)
            db.commit()
            from app.scheduled_delivery import latest_attempt
            retry_pending = latest_attempt(db, step_id).state == 'retryable'
        if retry_pending:
            _enable_delivery_recovery()

    async def send_and_record():
        from app.telegram import bot as tg_bot
        result = await send_exercise(tg_bot, claim.chat_id, claim.presentation, reply_markup=keyboard)
        # Even if the scheduler wait expires, this coroutine retains the receipt.
        for attempt in range(3):
            try:
                await asyncio.to_thread(persist, result)
                break
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(attempt + 1)
        if result.delivered:
            await asyncio.to_thread(reconcile_terminal_step_keyboards, [step_id])

        return result

    future = _submit_coroutine(send_and_record())
    if future is None:
        # The durable claim is deliberately quarantined if submission is lost.
        _enable_delivery_recovery()
        return
    try:
        return future.result(timeout=30)
    except Exception:
        _enable_delivery_recovery()
        logger.warning("Scheduled send outcome pending/unknown step=%s", step_id, exc_info=True)
        return ExerciseSendResult('uncertain', None, claim.presentation, '', failure_code='send_result_uncertain')


def reconcile_scheduled_deliveries():
    try:
        _recover_scheduled_deliveries()
    finally:
        _enable_delivery_recovery()


def _recover_scheduled_deliveries():
    """Recover due work and pending UI effects; never resend uncertain attempts."""
    from app.db import ExerciseDelivery
    from app.exercise_status import pending_status_step_ids
    from app.scheduled_delivery import LATE_GRACE, IN_FLIGHT_GRACE
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        due_ids = [row[0] for row in _due_delivery_steps(db, now).all()]
        # Quarantine interrupted sends independently of plan status or expiry.
        stale_ids = [row[0] for row in db.query(ExerciseDelivery.id).filter(
            ExerciseDelivery.state == 'in_flight', ExerciseDelivery.started_at <= now - IN_FLIGHT_GRACE,
        ).all()]
        retry_ids = [row[0] for row in db.query(ExerciseDelivery.plan_step_id).filter(ExerciseDelivery.state == 'retryable').distinct().all()]
        ui_ids = pending_status_step_ids(db)
    from app.scheduled_delivery import claim_send
    for step_id in set(retry_ids):
        with SessionLocal() as db:
            # claim_send also closes retries whose delivery window has ended.
            # Sending belongs to send_scheduled_message, not this cleanup pass.
            step = db.get(AIPlanStep, step_id)
            if step is not None and (step.step_status != 'pending' or not step.scheduled_for or now > _to_utc(step.scheduled_for) + LATE_GRACE or (step.expires_at and now >= _to_utc(step.expires_at))):
                claim_send(db, step_id, now=now)
                db.commit()
    for attempt_id in stale_ids:
        with SessionLocal() as db:
            attempt = db.get(ExerciseDelivery, attempt_id)
            if attempt:
                from app.lifecycle import _lock_user
                step = db.get(AIPlanStep, attempt.plan_step_id)
                _lock_user(db, step.day.plan.user_id, require_entitlement=False)
                db.refresh(attempt)
                if attempt.state == 'in_flight':
                    attempt.state = 'uncertain'
                    attempt.failure_code = 'interrupted_send'
                    db.commit()
    def recover_step(step_id):
        try:
            send_scheduled_message(0, '', step_id)
        except Exception:
            logger.exception("Scheduled recovery failed step=%s", step_id)
    if due_ids:
        # Reuse the same durable claim path with bounded parallel send waits.
        # One slow user cannot hold every subsequent due user behind it.
        with ThreadPoolExecutor(max_workers=8, thread_name_prefix='delivery-recovery') as workers:
            list(workers.map(recover_step, due_ids))
    reconcile_terminal_step_keyboards(ui_ids)


def _due_delivery_steps(db, now):
    from app.db import ExerciseDelivery
    from app.plan_completion.workflow import blocked_successor
    from app.scheduled_delivery import LATE_GRACE
    from sqlalchemy.orm import aliased
    later_attempt = aliased(ExerciseDelivery)
    return db.query(AIPlanStep.id).join(AIPlanDay).join(AIPlan).join(User).filter(
        ~blocked_successor(AIPlan.id), AIPlan.status == 'active', User.is_active.is_(True), AIPlanStep.step_status == 'pending',
        AIPlanStep.expires_at > now,
        AIPlanStep.scheduled_for <= now, AIPlanStep.scheduled_for >= now - LATE_GRACE,
        ~db.query(ExerciseDelivery.id).filter(
            ExerciseDelivery.plan_step_id == AIPlanStep.id,
            or_(ExerciseDelivery.state != 'retryable', ExerciseDelivery.next_attempt_at > now),
            ~db.query(later_attempt.id).filter(
                later_attempt.plan_step_id == ExerciseDelivery.plan_step_id,
                later_attempt.attempt > ExerciseDelivery.attempt
            ).correlate(ExerciseDelivery).exists()).exists(),
    )


def schedule_plan_step(step: AIPlanStep, user: User) -> bool:
    """
    Schedules a single step. Returns True if a NEW job was created.
    """
    if step.step_status in ("completed", "skipped", "expired", "canceled"):
        return False

    # We only schedule if we have a concrete time
    if not step.scheduled_for:
        return False
        
    if not user or not user.is_active:
        return False
    if not can_deliver_tasks(user, step.day.plan if step.day else None):
        return False

    if not step.day or not step.day.plan:
        return False
    if step.day.plan.status != "active":
        return False
    from app.plan_completion.workflow import blocked_successor
    from sqlalchemy.orm import object_session
    owning_db = object_session(step) if hasattr(step, "_sa_instance_state") else None
    if owning_db is not None and owning_db.query(blocked_successor(step.day.plan.id)).scalar():
        return False

    job_id = getattr(step, "job_id", None) or _generate_step_job_id(step)
    new_job_id_assigned = getattr(step, "job_id", None) is None

    # Ensure run_date is in the future
    run_date = step.scheduled_for.astimezone(pytz.UTC)
    now_utc = datetime.now(pytz.UTC)
    if run_date <= now_utc:
        return False

    logger.info("Scheduling job %s (replace_existing=True)", job_id)

    scheduler.add_job(
        "app.scheduler:send_scheduled_message",
        "date",
        id=job_id,
        run_date=run_date,
        args=[user.tg_id, "", step.id],
        replace_existing=True,
        misfire_grace_time=None,
        coalesce=False,
        max_instances=1,
    )

    return new_job_id_assigned


@dataclass(frozen=True)
class SchedulerReconciliation:
    attempted: int
    succeeded: int
    failed_ids: tuple[int, ...] = ()


def reconcile_plan_step_jobs(step_ids: list[int]) -> SchedulerReconciliation:
    """Idempotently ensure that every named active step has a scheduler job."""
    if not step_ids:
        return SchedulerReconciliation(attempted=0, succeeded=0)
    ordered_ids = tuple(dict.fromkeys(int(step_id) for step_id in step_ids))
    with SessionLocal() as db:
        rows = (
            db.query(AIPlanStep, AIPlanDay, AIPlan, User)
            .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
            .join(AIPlan, AIPlan.id == AIPlanDay.plan_id)
            .join(User, User.id == AIPlan.user_id)
            .filter(AIPlanStep.id.in_(ordered_ids))
            .all()
        )
        by_id = {step.id: (step, plan, user) for step, _, plan, user in rows}
        failed: list[int] = []
        succeeded = 0
        for step_id in ordered_ids:
            row = by_id.get(step_id)
            if row is None:
                failed.append(step_id)
                continue
            step, plan, user = row
            if str(plan.status) != "active" or getattr(user, "is_active", False) is not True:
                failed.append(step_id)
                continue
            try:
                schedule_plan_step(step, user)
                job_id = _generate_step_job_id(step)
                job = scheduler.get_job(job_id)
                expected_run_date = (
                    _to_utc(step.scheduled_for)
                    if step.scheduled_for is not None
                    else None
                )
                actual_run_date = getattr(job, "next_run_time", None)
                if actual_run_date is not None:
                    actual_run_date = _to_utc(actual_run_date)
                if job is None or actual_run_date != expected_run_date:
                    if job is not None:
                        try:
                            scheduler.remove_job(job_id)
                        except Exception:
                            pass
                    failed.append(step_id)
                    continue
            except Exception:
                failed.append(step_id)
                continue
            succeeded += 1
    return SchedulerReconciliation(
        attempted=len(ordered_ids),
        succeeded=succeeded,
        failed_ids=tuple(failed),
    )


def reconcile_plan_schedule(plan_id: int) -> SchedulerReconciliation:
    """Make jobs exact while fencing newer lifecycle decisions on the user row."""
    with SessionLocal() as db:
        initial_plan = (
            db.query(AIPlan)
            .filter(AIPlan.id == int(plan_id))
            .first()
        )
        if initial_plan is None:
            return SchedulerReconciliation(
                attempted=1,
                succeeded=0,
                failed_ids=(int(plan_id),),
            )
        # Every authoritative plan/time/cancel mutation takes this same row
        # lock.  Hold it across the external proof so an older reconciler
        # cannot write after a newer committed decision and report success.
        user = (
            db.query(User)
            .filter(User.id == initial_plan.user_id)
            .populate_existing()
            .with_for_update()
            .first()
        )
        plan = (
            db.query(AIPlan)
            .filter(AIPlan.id == int(plan_id))
            .populate_existing()
            .first()
        )
        if plan is None or user is None:
            return SchedulerReconciliation(
                attempted=1,
                succeeded=0,
                failed_ids=(int(plan_id),),
            )
        rows = (
            db.query(AIPlanStep)
            .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
            .filter(AIPlanDay.plan_id == plan.id)
            .order_by(AIPlanStep.id)
            .all()
        )
        from app.plan_completion.workflow import successor_delivery_blocked
        continuation_blocked = successor_delivery_blocked(db, plan.id)
        if continuation_blocked:
            from app.plan_completion.workflow import refresh_report_time
            refresh_report_time(db, user, plan.id)
            db.flush()
        now_utc = datetime.now(pytz.UTC)
        failed: list[int] = []
        succeeded = 0
        due_retry = False
        for step in rows:
            job_id = _generate_step_job_id(step)
            scheduled_for = (
                _to_utc(step.scheduled_for)
                if step.scheduled_for is not None
                else None
            )
            should_exist = (
                not continuation_blocked and str(plan.status) == "active"
                and user is not None
                and getattr(user, "is_active", False) is True
                and str(step.step_status) == "pending"
                and scheduled_for is not None
                and scheduled_for > now_utc
            )
            try:
                if should_exist:
                    schedule_plan_step(step, user)
                    job = scheduler.get_job(job_id)
                    actual = getattr(job, "next_run_time", None)
                    if actual is not None:
                        actual = _to_utc(actual)
                    if job is None or actual != scheduled_for:
                        raise RuntimeError("scheduler_job_not_exact")
                else:
                    job = scheduler.get_job(job_id)
                    if job is not None:
                        scheduler.remove_job(job_id)
                    if scheduler.get_job(job_id) is not None:
                        raise RuntimeError("obsolete_scheduler_job_present")
            except Exception:
                failed.append(int(step.id))
                continue
            succeeded += 1
        pending_ids = [step.id for step in rows if step.step_status == 'pending']
        if str(plan.status) == 'active' and user.is_active and pending_ids:
            from app.db import ExerciseDelivery
            due_retry = db.query(ExerciseDelivery.id).filter(
                ExerciseDelivery.plan_step_id.in_(pending_ids), ExerciseDelivery.state == 'retryable').first() is not None
        if continuation_blocked:
            db.commit()
    if continuation_blocked:
        enable_completion_recovery()
    if due_retry:
        _enable_delivery_recovery()
    return SchedulerReconciliation(
        attempted=len(rows),
        succeeded=succeeded,
        failed_ids=tuple(failed),
    )


def reconcile_cancel_plan_step_jobs(step_ids: list[int]) -> SchedulerReconciliation:
    """Idempotently prove that every named step job is absent."""
    if not step_ids:
        return SchedulerReconciliation(attempted=0, succeeded=0)
    ordered_ids = tuple(dict.fromkeys(int(step_id) for step_id in step_ids))
    with SessionLocal() as db:
        steps = db.query(AIPlanStep).filter(AIPlanStep.id.in_(ordered_ids)).all()
        by_id = {step.id: step for step in steps}
        failed: list[int] = []
        succeeded = 0
        for step_id in ordered_ids:
            step = by_id.get(step_id)
            if step is None:
                failed.append(step_id)
                continue
            job_id = _generate_step_job_id(step)
            try:
                scheduler.remove_job(job_id)
            except Exception:
                pass
            try:
                still_present = scheduler.get_job(job_id) is not None
            except Exception:
                failed.append(step_id)
                continue
            if still_present:
                failed.append(step_id)
                continue
            succeeded += 1
    return SchedulerReconciliation(
        attempted=len(ordered_ids),
        succeeded=succeeded,
        failed_ids=tuple(failed),
    )


def cancel_plan_step_jobs(step_ids: list[int]) -> int:
    """Compatibility count backed by the authoritative reconciliation result."""
    return reconcile_cancel_plan_step_jobs(step_ids).succeeded


def reschedule_plan_steps(step_ids: list[int]) -> int:
    """Compatibility count backed by the authoritative reconciliation result."""
    return reconcile_plan_step_jobs(step_ids).succeeded


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
        # Filter: Active User + Active Plan + Future Step + Not terminal
        pending_steps = (
            db.query(AIPlanStep, AIPlanDay, AIPlan, User)
            .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
            .join(AIPlan, AIPlan.id == AIPlanDay.plan_id)
            .join(User, User.id == AIPlan.user_id)
            .filter(
                AIPlan.status == "active",
                User.is_active == True,
                AIPlanStep.step_status == "pending",
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

    await asyncio.to_thread(expire_overdue_steps)
    await asyncio.to_thread(reconcile_scheduled_deliveries)
    await asyncio.to_thread(reconcile_plan_completions)

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
            active_users = _active_plan_users_query(db).all()

            sent_today_rows = (
                db.query(UserEvent.user_id)
                .filter(
                    UserEvent.event_name == "pulse_sent",
                    func.date(UserEvent.occurred_at) == today_utc,
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
                            event_source="scheduler",
                            source_operation_id=f"scheduler:pulse:{user.id}:{today_utc}",
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
        active_users = _active_plan_users_query(db).all()

        for user in active_users:
            try:
                # task_ignored is generated by the authoritative expiry
                # transition and must not count as user activity.
                # Only user-initiated activity counts as engagement.
                last_event = db.query(UserEvent).filter(
                    UserEvent.user_id == user.id,
                    UserEvent.event_name.in_(["task_completed", "task_skipped", "user_message"]),
                ).order_by(UserEvent.occurred_at.desc()).first()

                if not last_event:
                    continue

                days_silent = (now - _to_utc(last_event.occurred_at)).days
                if days_silent < 2:
                    continue

                sent_today = db.query(UserEvent).filter(
                    UserEvent.user_id == user.id,
                    UserEvent.event_name == "silent_sent",
                    func.date(UserEvent.occurred_at) == today,
                ).first()
                if sent_today:
                    continue

                recent_silent = db.query(UserEvent).filter(
                    UserEvent.user_id == user.id,
                    UserEvent.event_name == "silent_sent",
                    UserEvent.occurred_at >= now - timedelta(days=6),
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
                    event_source="scheduler",
                    source_operation_id=f"scheduler:silent:{user.id}:{today}",
                    context={"trigger": trigger_id, "days_silent": days_silent},
                )
                db.commit()
            except Exception:
                logger.error("[SILENT] user_id=%s", user.id, exc_info=True)


def reconcile_terminal_step_keyboards(
    step_ids: list[int],
) -> SchedulerReconciliation:
    """Retry terminal-step keyboard removal until PostgreSQL confirms it."""
    if not step_ids:
        return SchedulerReconciliation(attempted=0, succeeded=0)
    ordered_ids = tuple(dict.fromkeys(int(step_id) for step_id in step_ids))
    with SessionLocal() as db:
        rows = (
            db.query(AIPlanStep.id, AIPlanStep.tg_message_id, User.tg_id)
            .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
            .join(AIPlan, AIPlan.id == AIPlanDay.plan_id)
            .join(User, User.id == AIPlan.user_id)
            .filter(
                AIPlanStep.id.in_(ordered_ids),
                AIPlanStep.step_status.in_(
                    ("completed", "skipped", "expired", "canceled")
                ),
            )
            .all()
        )
    by_id = {step_id: (message_id, tg_id) for step_id, message_id, tg_id in rows}
    succeeded_ids: list[int] = []
    failed_ids: list[int] = []
    for step_id in ordered_ids:
        from app.exercise_status import reconcile_status
        try:
            with SessionLocal() as db:
                projected = reconcile_status(db, step_id)
        except Exception:
            logger.exception("Exercise status projection pending step=%s", step_id)
            projected = False
        if projected is not None:
            (succeeded_ids if projected else failed_ids).append(step_id)
            continue
        message_id, tg_id = by_id.get(step_id, (None, None))
        if message_id is None:
            succeeded_ids.append(step_id)
            continue
        if tg_id is None:
            failed_ids.append(step_id)
            continue

        async def _remove_keyboard(chat_id: int, telegram_message_id: int):
            from app.telegram import bot as tg_bot

            return await tg_bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=telegram_message_id,
                reply_markup=None,
            )

        future = _submit_coroutine(_remove_keyboard(tg_id, message_id))
        if future is None:
            failed_ids.append(step_id)
            continue
        try:
            future.result(timeout=30)
        except Exception as exc:
            # A callback may have removed the buttons before its DB marker
            # could be cleared. Telegram's exact no-change reply proves the
            # requested empty markup is already in place.
            if "message is not modified" not in str(exc).lower():
                failed_ids.append(step_id)
                continue
        with SessionLocal() as db:
            current = db.query(AIPlanStep).filter(AIPlanStep.id == step_id).first()
            if current is None:
                failed_ids.append(step_id)
                continue
            if current.tg_message_id == message_id:
                current.tg_message_id = None
                db.commit()
        succeeded_ids.append(step_id)

    if failed_ids:
        _enable_delivery_recovery()
    return SchedulerReconciliation(
        attempted=len(ordered_ids),
        succeeded=len(succeeded_ids),
        failed_ids=tuple(failed_ids),
    )


def reconcile_expired_step_keyboards(
    step_ids: list[int],
) -> SchedulerReconciliation:
    """Compatibility alias for the terminal keyboard reconciler."""
    return reconcile_terminal_step_keyboards(step_ids)


def expire_overdue_steps() -> None:
    """
    Runs every 30 seconds and at restart, using persisted deadlines.

    Expiry rule:
    - Primary: expires_at IS NOT NULL AND expires_at < now
    - Self-heal: if expires_at IS NULL, recompute it from scheduled_for using
      the user's local calendar day, then evaluate against now immediately.
      This keeps legacy / adaptation-created steps aligned with the same local
      end-of-day rule used by plan finalization.

    After marking expired, removes buttons only for steps expired in this run.
    Only pending/delivered steps can expire or emit ignored telemetry.
    """
    from sqlalchemy import or_
    from app.plan_completion.workflow import blocked_successor

    now_utc = datetime.now(pytz.UTC)

    with SessionLocal() as scan_db:
        candidate_ids = [
            step_id
            for (step_id,) in (
                scan_db.query(AIPlanStep.id)
                .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
                .join(AIPlan, AIPlan.id == AIPlanDay.plan_id)
                .join(User, User.id == AIPlan.user_id)
                .filter(
                    ~blocked_successor(AIPlan.id),
                    AIPlanStep.step_status.in_(["pending", "delivered"]),
                    or_(
                        AIPlanStep.expires_at.is_(None),
                        AIPlanStep.expires_at < now_utc,
                    ),
                )
                .order_by(AIPlanStep.id)
                .all()
            )
        ]

    repaired = 0
    count = 0
    failed_ids: list[int] = []
    expired_keyboard_ids: list[int] = []
    for step_id in candidate_ids:
        with SessionLocal() as candidate_db:
            step = (
                candidate_db.query(AIPlanStep)
                .filter(AIPlanStep.id == step_id)
                .first()
            )
            if step is None:
                continue
            repaired_candidate = False
            if step.expires_at is None and step.scheduled_for is not None:
                user_tz = resolve_timezone(
                    getattr(step.day.plan.user, "timezone", None)
                )
                step.expires_at = step_expires_at(step.scheduled_for, user_tz)
                repaired_candidate = True

            if step.expires_at is None or step.expires_at >= now_utc:
                if repaired_candidate:
                    candidate_db.commit()
                    repaired += 1
                continue

            try:
                expiry = expire_plan_step(
                    candidate_db,
                    user_id=step.day.plan.user_id,
                    step_id=step.id,
                    source_operation_id=(
                        f"scheduler:expiry:{step.id}:{step.expires_at.isoformat()}"
                    ),
                    occurred_at=step.expires_at,
                )
                candidate_db.commit()
                request_plan_completion(expiry.user_id, expiry.plan_id)
            except LifecycleTransitionError:
                candidate_db.rollback()
                continue
            except Exception:
                candidate_db.rollback()
                failed_ids.append(step_id)
                logger.exception(
                    "[EXPIRE] Candidate failed step=%s; continuing sweep",
                    step_id,
                )
                continue
            repaired += int(repaired_candidate)
            count += 1
            if step.tg_message_id is not None:
                expired_keyboard_ids.append(step_id)

    if count or repaired:
        logger.info(
            "[EXPIRE] Marked %d steps as expired, repaired %d missing expires_at values.",
            count,
            repaired,
        )
    if failed_ids:
        logger.error("[EXPIRE] Candidate failures for step ids: %s", failed_ids)

    if expired_keyboard_ids:
        reconciliation = reconcile_expired_step_keyboards(expired_keyboard_ids)
    else:
        reconciliation = SchedulerReconciliation(attempted=0, succeeded=0)
    if reconciliation.failed_ids:
        logger.warning(
            "[EXPIRE] Status projection pending for step ids: %s",
            reconciliation.failed_ids,
        )


def check_plan_completions() -> None:
    """FD-02 fixed UTC backstop, independent of normal user-local report timing."""
    reconcile_plan_completions()


PULSE_THRESHOLDS = {
    "MEDIUM": [7],
    "STANDARD": [7, 14],
    "LONG": [14, 28, 42, 56, 70, 84],
}


def _now_in_user_tz(user: User) -> datetime:
    tz = resolve_timezone(getattr(user, "timezone", None))
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
        if not can_deliver_tasks(user, plan):
            continue

        today = _now_in_user_tz(user).date()
        total_days = int(getattr(plan, "total_days", None) or 1)
        active_day = derive_current_day(db, plan.id, total_days)

        if active_day not in thresholds:
            continue

        already_sent = (
            db.query(UserEvent)
            .filter(
                UserEvent.user_id == plan.user_id,
                UserEvent.event_name == "pulse_sent",
                UserEvent.plan_id == plan.id,
                UserEvent.properties["active_day"].astext == str(active_day),
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
            event_source="scheduler",
            source_operation_id=f"scheduler:pulse-snapshot:{plan.id}:{active_day}",
            plan_id=plan.id,
            context={"active_day": active_day, "date": str(today)},
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

# Completion recovery has an independent in-memory alarm. Its work/receipts live
# in PostgreSQL; the alarm parks at the earliest unfinished obligation deadline.
_completion_recovery_lock = Lock()
_completion_source_failure = False


def _terminal_completion_sources(db):
    from app.lifecycle import TERMINAL_STEP_STATUSES
    child = db.query(AIPlanStep.id).join(AIPlanDay).filter(AIPlanDay.plan_id == AIPlan.id)
    return db.query(AIPlan.user_id, AIPlan.id).join(User).filter(
        AIPlan.status == 'active', User.is_active.is_(True), child.exists(),
        ~child.filter(AIPlanStep.step_status.notin_(TERMINAL_STEP_STATUSES)).exists(),
    )


def _completion_recovery_time(now):
    from app.db import PlanCompletion
    with SessionLocal() as db:
        wake = db.query(func.min(PlanCompletion.wake_at)).filter(or_(
            PlanCompletion.state.in_(['pending', 'in_flight', 'release_in_flight']),
            PlanCompletion.schedule_pending.is_(True),
        )).scalar()
    if _completion_source_failure:
        retry = now + timedelta(minutes=5)
        wake = min(_to_utc(wake), retry) if wake else retry
    return max(now + timedelta(seconds=1), _to_utc(wake)) if wake else None


def enable_completion_recovery():
    with _completion_recovery_lock:
        now = datetime.now(timezone.utc)
        try:
            wake = _completion_recovery_time(now)
        except Exception:
            logger.exception('Completion recovery DB unavailable')
            wake = now + timedelta(seconds=30)
        job_id = 'plan_completion_recovery'
        job = scheduler.get_job(job_id, jobstore='completion_recovery')
        if wake is None:
            if job is not None:
                scheduler.remove_job(job_id, jobstore='completion_recovery')
        elif job is not None:
            scheduler.modify_job(job_id, jobstore='completion_recovery', next_run_time=wake)
        else:
            scheduler.add_job('app.scheduler:reconcile_plan_completions', 'interval',
                seconds=30, start_date=wake, next_run_time=wake,
                id=job_id, jobstore='completion_recovery', replace_existing=True,
                max_instances=1, coalesce=True, misfire_grace_time=None)


def request_plan_completion(user_id, plan_id):
    """Post-commit action/expiry entrance; failures retain terminal source facts."""
    global _completion_source_failure
    from app.orchestrator import _auto_complete_plan_if_needed, send_plan_completion_message
    try:
        with SessionLocal() as db:
            user = db.get(User, user_id)
            if user is None:
                return
            result = _auto_complete_plan_if_needed(db, user, expected_plan_id=plan_id)
            db.commit()
        if result is not None:
            # Normal completion is fast, but no-action waits for the durable due
            # time. claim_report is the sole arbiter even when called early.
            coro = send_plan_completion_message(user_id, plan_id)
            if _submit_coroutine(coro) is None:
                coro.close()
    except Exception:
        _completion_source_failure = True
        logger.exception('Explicit completion pending user=%s plan=%s', user_id, plan_id)
    finally:
        enable_completion_recovery()


def reconcile_plan_completions():
    """Safety Car: unfinished sources/obligations only, no repaired user sessions."""
    global _completion_source_failure
    from app.db import PlanCompletion
    from app.orchestrator import send_plan_completion_message
    now = datetime.now(timezone.utc)
    try:
        # Terminal source scan is narrow SQL and opens a session only per actual
        # unfinished source, never per historical completed plan.
        with SessionLocal() as db:
            sources = _terminal_completion_sources(db).all()
        _completion_source_failure = False
        for user_id, plan_id in sources:
            request_plan_completion(user_id, plan_id)
        with SessionLocal() as db:
            due = db.query(PlanCompletion.plan_id, AIPlan.user_id).join(
                AIPlan, AIPlan.id == PlanCompletion.plan_id).filter(
                    PlanCompletion.state.in_(['pending', 'in_flight', 'release_in_flight']),
                    PlanCompletion.wake_at <= now).all()
            schedules = db.query(PlanCompletion.plan_id, PlanCompletion.successor_plan_id).filter(
                PlanCompletion.schedule_pending.is_(True), PlanCompletion.wake_at <= now).all()
        def dispatch(pair):
            user_id, plan_id = pair[1], pair[0]
            coro = send_plan_completion_message(user_id, plan_id)
            future = _submit_coroutine(coro)
            if future is None:
                coro.close()
                return
            try:
                future.result(timeout=45)
            except Exception:
                logger.exception('Completion dispatch unconfirmed plan=%s', plan_id)
        if due:
            with ThreadPoolExecutor(max_workers=8, thread_name_prefix='completion') as workers:
                list(workers.map(dispatch, due))
        for source_id, successor_id in schedules:
            result = reconcile_plan_schedule(successor_id)
            with SessionLocal() as db:
                # Reconciliation itself fences the actual current job set using
                # the user lock. No lock is held while waiting on another session.
                row = db.get(PlanCompletion, source_id)
                row.schedule_pending = bool(result.failed_ids)
                if not row.schedule_pending:
                    from app.telemetry import write_event_operation
                    successor = db.get(AIPlan, successor_id)
                    write_event_operation(db, user_id=successor.user_id,
                        event_name='plan_activated', event_source='plan_finalization',
                        source_operation_id=f'plan-activation:{successor_id}', plan_id=successor_id,
                        occurred_at=successor.activated_at, properties={'total_days':successor.total_days})
                if row.schedule_pending:
                    row.wake_at = datetime.now(timezone.utc) + timedelta(seconds=30)
                db.commit()
    except Exception:
        logger.exception('Completion recovery interrupted')
        _completion_source_failure = True
    finally:
        enable_completion_recovery()
