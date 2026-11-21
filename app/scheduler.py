# app/scheduler.py
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import logging
from threading import BoundedSemaphore, Event, Lock
from math import ceil
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytz
from sqlalchemy import and_, case, func, or_, text as sql_text, tuple_
from apscheduler.executors.pool import ThreadPoolExecutor as SchedulerExecutor
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.blocking_db import BlockingLane, DBBusyError, fail_fast_on_deployment_lock
from app.config import settings
from app.runtime_jobstore import DeliveryJobExecutor, RuntimeJobStore
from app.active_days import resolve_timezone, step_expires_at
from app.plan_completion.tokens import issue_report_grant
from app.db import AIPlan, AIPlanDay, AIPlanStep, ReceiptSessionLocal, SessionLocal, User, UserEvent
from app.ai import async_client
from app.telemetry import log_user_event, write_event_operation
from app.ux.catalog import get_trigger_message
from app.ux.persona import get_persona
from app.ux.pulse_prompt import generate_pulse_message
from app.ux.rate_limit import can_send_auto_message
from app.exercise_delivery import ExerciseSendResult, send_exercise
from app.step_action_ingress import has_timely_step_action
from app.lifecycle import (
    LifecycleTransitionError,
    derive_current_day,
    expire_plan_step,
)

# Configure JobStore
DATABASE_URL = settings.DATABASE_URL
jobstore_url = DATABASE_URL

_DELIVERY_WORKERS = 4
_DELIVERY_RECOVERY_WORKERS = 2
_delivery_overflow = Event()
_delivery_executor = DeliveryJobExecutor(_DELIVERY_WORKERS, _delivery_overflow)

jobstores = {
    "default": RuntimeJobStore(url=jobstore_url, delivery_capacity=_delivery_executor.available, engine_options={
        "pool_size": 4, "max_overflow": 0, "pool_timeout": 2,
        "connect_args": {"connect_timeout": 3},
    }),
    "delivery_recovery": MemoryJobStore(),
    "completion_recovery": MemoryJobStore(),
    "maintenance": MemoryJobStore(),
}
from app.access import AccessDenied, access_predicate, may_access, outbound_claim

scheduler = BackgroundScheduler(
    jobstores=jobstores, timezone="UTC",
    executors={
        "default": SchedulerExecutor(5),
        "scheduled_delivery": _delivery_executor,
    },
)
_application_scheduler = scheduler

logger = logging.getLogger(__name__)

# TECH-DEBT TD-3:
# Never reuse ORM objects across SQLAlchemy sessions.
# Always re-fetch entities inside the session where they are used.

_event_loop: Optional[asyncio.AbstractEventLoop] = None
_recovery_job_lock = Lock()
_DELIVERY_SCAN_BATCH = 16
_delivery_slots = BoundedSemaphore(_DELIVERY_WORKERS)
_delivery_recovery_slots = BoundedSemaphore(_DELIVERY_RECOVERY_WORKERS)
_delivery_receipts = BlockingLane(_DELIVERY_WORKERS + _DELIVERY_RECOVERY_WORKERS, "delivery-receipt")
_recovery_workers = BlockingLane(_DELIVERY_RECOVERY_WORKERS, "delivery-recovery")
_projection_recovery = BlockingLane(2, "projection-recovery")
_recovery_cursors = {"stale": 0, "retry": 0, "keyboard": 0}
_due_after = None

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
    # Remove persisted jobs for retired runtime entrances. APScheduler owns its
    # job table, so cleanup stays at the scheduler boundary rather than Alembic.
    for retired_job_id in (
        "daily_pulse",
        "silent_check",
        "pulse_snapshot_check",
        "stuck_schedule_adj_check",
        "ignored_check",
        "scheduled_delivery_recovery",
    ):
        try:
            scheduler.remove_job(retired_job_id)
        except Exception:
            pass
    if scheduler is _application_scheduler:
        jobstores["default"].remove_legacy_completion_jobs()
    else:
        for job in scheduler.get_jobs():
            if job.id.startswith(("completion_", "completion_retry_", "completion_receipt_retry_")):
                remove_job(job.id)
    if scheduler is _application_scheduler:
        for moved in ("expire_steps", "plan_completion_check", "privacy_maintenance"):
            remove_job(moved)
        scheduler.add_job("app.scheduler:dispatch_plan_schedule_recovery", "interval", seconds=1,
                          id="plan_schedule_recovery", jobstore="maintenance", max_instances=1,
                          coalesce=True, replace_existing=True)
    maintenance = {"jobstore": "maintenance"} if scheduler is _application_scheduler else {}
    privacy_timing = {**maintenance, "next_run_time": datetime.now(timezone.utc)} if maintenance else {}
    scheduler.add_job("app.scheduler:expire_overdue_steps", "interval", seconds=30, id="expire_steps", replace_existing=True, max_instances=1, **maintenance)
    scheduler.add_job("app.scheduler:check_plan_completions", "cron", hour=10, minute=30, id="plan_completion_check", replace_existing=True, max_instances=1, **maintenance)
    scheduler.add_job("app.scheduler:run_privacy_maintenance", "interval", minutes=5, id="privacy_maintenance", replace_existing=True, max_instances=1, **privacy_timing)
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
        coro.close()
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
                              id='scheduled_delivery_recovery', jobstore='delivery_recovery', kwargs={'wait': False},
                              replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=None)


def wake_scheduled_delivery_recovery():
    """After a committed delivery resume, wake any due work without blocking on sends."""
    _enable_delivery_recovery()
    with _recovery_job_lock:
        if scheduler.get_job('scheduled_delivery_recovery', jobstore='delivery_recovery') is not None:
            scheduler.modify_job('scheduled_delivery_recovery', jobstore='delivery_recovery',
                                 next_run_time=datetime.now(timezone.utc))


def _delivery_recovery_time(now):
    from app.db import ExerciseDelivery
    from app.exercise_status import pending_status_steps
    from app.scheduled_delivery import IN_FLIGHT_GRACE, LATE_GRACE
    with SessionLocal() as db:
        retry_at = db.query(func.min(case(
                (AIPlanStep.step_status != 'pending', now),
                (and_(AIPlan.status == 'active', and_(User.is_active.is_(True), access_predicate(User.id, delivery=True))), func.greatest(ExerciseDelivery.next_attempt_at, AIPlanStep.scheduled_for)),
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
        ready = pending_status_steps(db).first() is not None or _due_delivery_steps(db, now).first() is not None
    if ready:
        return now + timedelta(seconds=1)
    return max(now + timedelta(seconds=30), min(times)) if times else None


def can_deliver_tasks(user: User, plan: AIPlan | None = None, *, delivery: bool = True) -> bool:
    # NOTE:
    # Missed scheduled deliveries while user is not ACTIVE are intentional.
    # Replay/recovery (if any) must be handled by adaptation/reconciliation layer.
    if not user or not user.is_active:
        return False
    from sqlalchemy.orm import object_session
    owning_db = object_session(user) if hasattr(user, '_sa_instance_state') else None
    if owning_db is None or not may_access(owning_db, user.id, delivery=delivery):
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
            and_(User.is_active.is_(True), access_predicate(User.id, delivery=True)),
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


def send_scheduled_message(_chat_id: int, text: str, step_id: int | None = None,
                           *, recovery: bool = False, wait_for_receipt: bool = False):
    """Claim once before Telegram; persist late results inside the send coroutine."""
    if step_id is None or _event_loop is None:
        return
    slots = _delivery_recovery_slots if recovery else _delivery_slots
    if not slots.acquire(blocking=False):
        # No attempt was claimed. The durable due row remains recoverable.
        _enable_delivery_recovery()
        return
    future = None
    try:
        from app.scheduled_delivery import claim_send, finish_send
        from app.active_days import resolve_active_days, is_active_day
        with SessionLocal() as db:
            db.execute(sql_text("SET LOCAL lock_timeout = '3s'"))
            db.execute(sql_text("SET LOCAL statement_timeout = '10s'"))
            step = db.get(AIPlanStep, step_id)
            if step is None:
                return
            user = step.day.plan.user
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
            from app.db import ExerciseDelivery
            with ReceiptSessionLocal() as db:
                db.execute(sql_text("SET LOCAL lock_timeout = '3s'"))
                db.execute(sql_text("SET LOCAL statement_timeout = '10s'"))
                finish_send(db, claim.attempt_id, result)
                attempt = db.get(ExerciseDelivery, claim.attempt_id)
                retry_pending = attempt is not None and attempt.state == 'retryable'
                step = db.get(AIPlanStep, step_id) if result.delivered else None
                late_projection = step is not None and step.step_status in (
                    "completed", "skipped", "expired", "canceled",
                )
                db.commit()
            if retry_pending or late_projection:
                _enable_delivery_recovery()

        async def send_and_record():
            from app.telegram import bot as tg_bot
            try:
                result = await asyncio.wait_for(
                    send_exercise(tg_bot, claim.chat_id, claim.presentation, reply_markup=keyboard), timeout=20,
                )
            except TimeoutError:
                result = ExerciseSendResult('uncertain', None, claim.presentation, '', failure_code='send_result_uncertain')
            # Even if the scheduler wait expires, this coroutine retains the receipt.
            for attempt in range(3):
                try:
                    await _delivery_receipts.run(persist, result)
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(attempt + 1)
            return result

        future = _submit_coroutine(send_and_record())
        if future is None:
            # The durable claim is deliberately quarantined if submission is lost.
            _enable_delivery_recovery()
            return
        try:
            return future.result(timeout=None if wait_for_receipt else 30)
        except Exception:
            _enable_delivery_recovery()
            logger.warning("Scheduled send outcome pending/unknown step=%s", step_id, exc_info=True)
            return ExerciseSendResult('uncertain', None, claim.presentation, '', failure_code='send_result_uncertain')
    except Exception:
        _delivery_overflow.set()
        raise
    finally:
        if future is None:
            slots.release()
        else:
            future.add_done_callback(lambda _done: slots.release())


def reconcile_scheduled_deliveries(*, wait=True):
    try:
        _recover_scheduled_deliveries(wait=wait)
    finally:
        _enable_delivery_recovery()


def _recovery_page(query, column, cursor_name):
    after = _recovery_cursors[cursor_name]
    page = [row[0] for row in query.filter(column > after).order_by(column).limit(_DELIVERY_SCAN_BATCH)]
    if not page and after:
        page = [row[0] for row in query.order_by(column).limit(_DELIVERY_SCAN_BATCH)]
    _recovery_cursors[cursor_name] = page[-1] if len(page) == _DELIVERY_SCAN_BATCH else 0
    return page


def _recover_scheduled_deliveries(*, wait):
    """Recover bounded pages and never resend an uncertain attempt."""
    from app.db import ExerciseDelivery
    from app.exercise_status import pending_status_steps
    from app.scheduled_delivery import LATE_GRACE, IN_FLIGHT_GRACE
    global _due_after
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        due = (_due_delivery_steps(db, now).add_columns(AIPlanStep.scheduled_for)
               .order_by(AIPlanStep.scheduled_for, AIPlanStep.id))
        page = due if _due_after is None else due.filter(
            tuple_(AIPlanStep.scheduled_for, AIPlanStep.id) > _due_after)
        capacity = _recovery_workers.available()
        due_rows = page.limit(capacity).all() if capacity else []
        if capacity and not due_rows and _due_after is not None:
            due_rows = due.limit(capacity).all()
        due_ids = [step_id for step_id, _ in due_rows]
        _due_after = (due_rows[-1][1], due_ids[-1]) if due_rows and len(due_rows) == capacity else None
        stale_ids = _recovery_page(db.query(ExerciseDelivery.id).filter(
            ExerciseDelivery.state == 'in_flight', ExerciseDelivery.started_at <= now - IN_FLIGHT_GRACE,
        ), ExerciseDelivery.id, "stale")
        retry_ids = _recovery_page(db.query(ExerciseDelivery.plan_step_id).join(AIPlanStep).filter(
            ExerciseDelivery.state == 'retryable',
            or_(AIPlanStep.step_status != 'pending', AIPlanStep.scheduled_for.is_(None),
                AIPlanStep.scheduled_for < now - LATE_GRACE, AIPlanStep.expires_at <= now),
        ).distinct(), ExerciseDelivery.plan_step_id, "retry")
        ui_ids = _recovery_page(pending_status_steps(db), ExerciseDelivery.plan_step_id, "keyboard")
    def recover_step(step_id):
        try:
            send_scheduled_message(0, "", step_id, recovery=True, wait_for_receipt=not wait)
        except Exception:
            logger.exception("Scheduled recovery failed step=%s", step_id)

    pending = []
    for step_id in due_ids:
        try:
            pending.append(_recovery_workers.submit(recover_step, step_id))
        except DBBusyError:
            break
    if wait:
        for future in pending:
            future.result()

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
    if wait:
        reconcile_terminal_step_keyboards(ui_ids)
    else:
        for step_id in ui_ids:
            try:
                _projection_recovery.submit(reconcile_terminal_step_keyboards, [step_id])
            except DBBusyError:
                break



def _due_delivery_steps(db, now):
    from app.db import ExerciseDelivery
    from app.scheduled_delivery import LATE_GRACE
    from sqlalchemy.orm import aliased
    later_attempt = aliased(ExerciseDelivery)
    return db.query(AIPlanStep.id).join(AIPlanDay).join(AIPlan).join(User).filter(
        AIPlan.status == 'active', and_(User.is_active.is_(True), access_predicate(User.id, delivery=True)), AIPlanStep.step_status == 'pending',
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
    # The operator switch fences the durable send claim. Keep future timers
    # exact even while delivery is disabled so a resume preserves their dates.
    if not can_deliver_tasks(user, step.day.plan if step.day else None, delivery=False):
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

    scheduler.add_job(
        "app.scheduler:send_scheduled_message",
        "date",
        id=job_id,
        run_date=run_date,
        args=[user.tg_id, "", step.id],
        replace_existing=True,
        executor="scheduled_delivery" if scheduler is _application_scheduler else "default",
        kwargs={"wait_for_receipt": True} if scheduler is _application_scheduler else {},
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


def reconcile_plan_schedule(plan_id: int, *, skip_busy_owner: bool = False) -> SchedulerReconciliation:
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
            .with_for_update(skip_locked=skip_busy_owner)
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
        from app.db import PlanCompletion
        completion = db.query(PlanCompletion).filter(PlanCompletion.successor_plan_id == plan.id).first()
        if completion is not None and completion.legacy_schedule_repair:
            from app.db import ExerciseDelivery
            from app.lifecycle import _completion_time_slots
            from app.scheduled_delivery import LATE_GRACE
            from app.time_slots import reanchor_pending_plan_steps

            now = datetime.now(timezone.utc)
            attempted = {
                step_id for step_id, in db.query(ExerciseDelivery.plan_step_id)
                .join(AIPlanStep).join(AIPlanDay).filter(AIPlanDay.plan_id == plan.id)
            }
            unattempted = [
                step for day in plan.days for step in day.steps
                if step.step_status == 'pending' and step.id not in attempted
            ]
            if plan.status == 'active' and any(
                step.scheduled_for is not None and _to_utc(step.scheduled_for) < now - LATE_GRACE
                for step in unattempted
            ):
                reanchor_pending_plan_steps(
                    user, plan, resumed_at=now, daily_time_slots=_completion_time_slots(user, plan),
                    eligible_step_ids={step.id for step in unattempted},
                )
                plan.version += 1
            completion.legacy_schedule_repair = False
            db.flush()
        rows = (
            db.query(AIPlanStep)
            .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
            .filter(AIPlanDay.plan_id == plan.id)
            .order_by(AIPlanStep.id)
            .all()
        )
        now_utc = datetime.now(pytz.UTC)
        failed: list[int] = []
        succeeded = 0
        wake_delivery = False
        for step in rows:
            job_id = _generate_step_job_id(step)
            scheduled_for = (
                _to_utc(step.scheduled_for)
                if step.scheduled_for is not None
                else None
            )
            should_exist = (
                str(plan.status) == "active"
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
            has_due = any(
                step.step_status == "pending" and step.scheduled_for is not None
                and getattr(step, "expires_at", None) is not None
                and _to_utc(step.scheduled_for) <= now_utc < _to_utc(step.expires_at)
                for step in rows
            )
            wake_delivery = has_due or db.query(ExerciseDelivery.id).filter(
                ExerciseDelivery.plan_step_id.in_(pending_ids), ExerciseDelivery.state == "retryable",
            ).first() is not None
        repair_pending = getattr(plan, "schedule_reconcile_at", None) is not None
        is_switch = getattr(plan, "schedule_switch_source", None) is not None
        if ((completion is not None or (repair_pending and not is_switch)) and not failed and user.is_active
                and str(plan.status) == 'active'
                and any(step.step_status == 'pending' and step.scheduled_for is not None
                        and _to_utc(step.scheduled_for) > now_utc for step in rows)):
            from app.telemetry import write_event_operation
            try:
                with db.begin_nested():
                    # Preserve the first successful proof on duplicate writes.
                    write_event_operation(db, user_id=user.id,
                        event_name='plan_activated', event_source='plan_finalization',
                        source_operation_id=f'plan-activation:{plan.id}', plan_id=plan.id,
                        properties={'total_days': plan.total_days})
            except Exception:
                recheck = datetime.now(timezone.utc) + timedelta(seconds=30)
                if completion is not None:
                    completion.schedule_pending = True
                    completion.schedule_wake_at = recheck
                if repair_pending:
                    plan.schedule_reconcile_at = recheck
                db.commit()
                if completion is not None:
                    enable_completion_recovery()
                raise
        if completion is not None:
            # Clear/retry the obligation under the same lock as its proof;
            # an older scanner must not erase a newer resume's owed work.
            completion.schedule_pending = bool(failed)
            completion.schedule_wake_at = datetime.now(timezone.utc) + timedelta(seconds=30) if failed else None
        if repair_pending and not is_switch:
            plan.schedule_reconcile_at = datetime.now(timezone.utc) + timedelta(seconds=15) if failed else None
        if completion is not None or repair_pending:
            db.commit()
    if wake_delivery:
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


_RESTORE_BATCH = 32


def _restore_future_job_batch(after):
    """Read a page in deadline order. Each step is re-read under its owner fence."""
    now = datetime.now(pytz.UTC)
    with SessionLocal() as db:
        query = (db.query(AIPlanStep.scheduled_for, AIPlanStep.id, AIPlan.user_id, AIPlan.id)
            .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
            .join(AIPlan, AIPlan.id == AIPlanDay.plan_id)
            .join(User, User.id == AIPlan.user_id)
            .filter(AIPlan.status == "active", User.is_active.is_(True), access_predicate(User.id),
                    AIPlanStep.step_status == "pending", AIPlanStep.scheduled_for > now))
        if after is not None:
            query = query.filter(tuple_(AIPlanStep.scheduled_for, AIPlanStep.id) > after)
        rows = query.order_by(AIPlanStep.scheduled_for, AIPlanStep.id).limit(_RESTORE_BATCH).all()

    failed_plans = set()
    for _, step_id, user_id, plan_id in rows:
        try:
            with SessionLocal() as db:
                user = db.query(User).filter_by(id=user_id).with_for_update(skip_locked=True).first()
                if user is None:
                    failed_plans.add(plan_id)
                    continue
                step = db.get(AIPlanStep, step_id)
                if not user.is_active or step is None or step.step_status != "pending":
                    continue
                if (step.day.plan.status != "active" or not step.scheduled_for
                        or step.scheduled_for <= datetime.now(pytz.UTC)):
                    continue
                job_id = _generate_step_job_id(step)
                existing = scheduler.get_job(job_id)
                if (existing is None or existing.next_run_time != step.scheduled_for
                        or (scheduler is _application_scheduler and existing.executor != "scheduled_delivery")):
                    with fail_fast_on_deployment_lock():
                        schedule_plan_step(step, user)
        except DBBusyError:
            failed_plans.add(plan_id)
        except Exception:
            failed_plans.add(plan_id)
            logger.exception("Scheduled timer restoration failed step=%s", step_id)
    rescan = False
    if failed_plans:
        with SessionLocal.begin() as db:
            plans = db.query(AIPlan).filter(AIPlan.id.in_(failed_plans)).with_for_update(skip_locked=True).all()
            for plan in plans:
                if plan.status == "active" and plan.schedule_reconcile_at is None:
                    plan.schedule_reconcile_at = now
            rescan = len(plans) != len(failed_plans)
    after = (rows[-1][0], rows[-1][1]) if len(rows) == _RESTORE_BATCH else None
    return after, rescan


async def start_scheduler():
    """Initialize the scheduler before serving traffic."""
    global _event_loop
    _event_loop = asyncio.get_running_loop()
    await asyncio.to_thread(init_scheduler)


async def restore_scheduled_timers():
    """Restore alarms and timers without gating new requests on scans."""
    await asyncio.gather(
        asyncio.to_thread(expire_overdue_steps),
        asyncio.to_thread(_enable_delivery_recovery),
        asyncio.to_thread(enable_completion_recovery),
    )

    after = None
    rescan = False
    while True:
        try:
            after, deferred = await asyncio.to_thread(_restore_future_job_batch, after)
            rescan |= deferred
        except Exception:
            logger.exception("Scheduled timer restoration interrupted")
            await asyncio.sleep(5)
            continue
        if after is None:
            await asyncio.to_thread(_enable_delivery_recovery)
            if not rescan:
                break
            # A transaction held both the owner and the repair marker. Finish
            # the pass for other owners before retrying from durable state.
            rescan = False
            await asyncio.sleep(5)


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
                    claim_at = await asyncio.to_thread(outbound_claim, user_id, delivery=True)
                    if claim_at is None:
                        return
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
                            attribution_at=claim_at,
                        )
                        db.commit()
                except Exception:
                    logger.error("[PULSE] user_id=%s", user_id, exc_info=True)

        await asyncio.gather(*[_send_one(uid) for uid in pending_ids])
        logger.info("[PULSE] Completed for %d users", len(pending_ids))

    asyncio.run_coroutine_threadsafe(_run(), _event_loop)


def _record_silent_attempt(user_id, trigger_id, days_silent, operation_id):
    with SessionLocal() as db:
        result = write_event_operation(
            db, user_id=user_id, event_name="silent_send_attempted",
            event_source="scheduler", source_operation_id=operation_id + ":attempt",
            properties={"trigger": trigger_id, "days_silent": days_silent},
        )
        db.commit()
        return not result.duplicate


def _record_silent_receipt(user_id, trigger_id, days_silent, operation_id, claim_at):
    with SessionLocal() as db:
        log_user_event(
            db, user_id=user_id, event_type="silent_sent",
            event_source="scheduler", source_operation_id=operation_id + ":sent",
            context={"trigger": trigger_id, "days_silent": days_silent},
            attribution_at=claim_at,
        )
        db.commit()


async def _deliver_silent_reminder(user_id, tg_id, msg, trigger_id, days_silent, operation_id):
    try:
        # The attempt is durable before Telegram I/O. An unconfirmed attempt
        # shares the existing six-day cooldown without claiming a known send.
        if not await asyncio.to_thread(
            _record_silent_attempt, user_id, trigger_id, days_silent, operation_id
        ):
            return False
        # Run inside the queued coroutine, after the attempt write and just
        # before dispatch; no deployment lock is held across network I/O.
        claim_at = await asyncio.to_thread(outbound_claim, user_id, delivery=True)
        if claim_at is None:
            return False
        result = await _send_message_async(tg_id, msg)
        if not result:
            return False
        # This coroutine owns the receipt even after the scheduler's 30-second
        # wait expires. A failed write is visible and the attempt still fences
        # another automatic reminder during the cooldown.
        await asyncio.to_thread(
            _record_silent_receipt, user_id, trigger_id, days_silent, operation_id, claim_at
        )
        return True
    except Exception:
        logger.exception("[SILENT] Delivery or receipt failed user_id=%s", user_id)
        return False


def check_silent_users():
    """Runs at 12:00 UTC. One re-engagement attempt per six-day window."""
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
                    UserEvent.event_name.in_(["silent_sent", "silent_send_attempted"]),
                    func.date(UserEvent.occurred_at) == today,
                ).first()
                if sent_today:
                    continue

                recent_silent = db.query(UserEvent).filter(
                    UserEvent.user_id == user.id,
                    UserEvent.event_name.in_(["silent_sent", "silent_send_attempted"]),
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

                operation_id = f"scheduler:silent:{user.id}:{today}"
                future = _submit_coroutine(_deliver_silent_reminder(
                    user.id, user.tg_id, msg, trigger_id, days_silent, operation_id
                ))
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


_EXPIRY_BATCH = 32
_expiry_after = 0
_expiry_lock = Lock()


def expire_overdue_steps() -> None:
    if not _expiry_lock.acquire(blocking=False):
        return
    try:
        if _delivery_overflow.is_set():
            _delivery_overflow.clear()
            _enable_delivery_recovery()
        _expire_overdue_batch()
    finally:
        _expiry_lock.release()


def _expire_overdue_batch() -> None:
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
    from sqlalchemy import exists, or_

    global _expiry_after
    from app.db import PlanCompletion
    legacy_repair_pending = (
        PlanCompletion.successor_plan_id == AIPlan.id,
        PlanCompletion.legacy_schedule_repair.is_(True),
    )
    now_utc = datetime.now(pytz.UTC)

    with SessionLocal() as scan_db:
        candidates = (
            scan_db.query(AIPlanStep.id, AIPlan.user_id)
            .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
            .join(AIPlan, AIPlan.id == AIPlanDay.plan_id)
            .join(User, User.id == AIPlan.user_id)
            .filter(
                or_(AIPlanStep.step_status != "pending", ~exists().where(*legacy_repair_pending)),
                access_predicate(User.id),
                AIPlanStep.id > _expiry_after,
                AIPlanStep.step_status.in_(["pending", "delivered"]),
                or_(
                    and_(AIPlanStep.expires_at.is_(None), AIPlanStep.scheduled_for.is_not(None)),
                    AIPlanStep.expires_at < now_utc,
                ),
            )
            .order_by(AIPlanStep.id)
            .limit(_EXPIRY_BATCH)
            .all()
        )
    _expiry_after = candidates[-1][0] if len(candidates) == _EXPIRY_BATCH else 0

    repaired = 0
    count = 0
    failed_ids: list[int] = []
    expired_keyboard_ids: list[int] = []
    for step_id, user_id in candidates:
        with SessionLocal() as candidate_db:
            # Skip an owner already busy with a callback or another transaction.
            # The step remains eligible for a later sweep.
            owner = (candidate_db.query(User).filter(User.id == user_id)
                     .with_for_update(skip_locked=True).first())
            if owner is None:
                continue
            step = (
                candidate_db.query(AIPlanStep)
                .filter(AIPlanStep.id == step_id)
                .first()
            )
            if step is None or step.step_status not in {"pending", "delivered"}:
                continue
            repaired_candidate = False
            if step.expires_at is None and step.scheduled_for is not None:
                user_tz = resolve_timezone(owner.timezone)
                step.expires_at = step_expires_at(step.scheduled_for, user_tz)
                repaired_candidate = True

            if step.expires_at is None or step.expires_at >= now_utc:
                if repaired_candidate:
                    candidate_db.commit()
                    repaired += 1
                continue

            if has_timely_step_action(owner.tg_id, step_id, _to_utc(step.expires_at)):
                continue

            try:
                expiry = expire_plan_step(
                    candidate_db,
                    user_id=user_id,
                    step_id=step.id,
                    source_operation_id=(
                        f"scheduler:expiry:{step.id}:{step.expires_at.isoformat()}"
                    ),
                    occurred_at=step.expires_at,
                )
                candidate_db.commit()
                _submit_continuation(expiry.user_id, expiry.plan_id)
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

    if count:
        # A saturated lane left the terminal DB facts ready for the next pass.
        enable_completion_recovery()
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
    # Primitive scan hints survive a per-owner rollback; current access and
    # plan ownership are still checked by grant issuance before every send.
    active_plans = [(p.id, p.user_id, p.duration, p.total_days)
        for p in db.query(AIPlan).filter(AIPlan.status == "active").all()]

    for plan_id, user_id, duration, total_days_hint in active_plans:
        thresholds = PULSE_THRESHOLDS.get(duration, [])
        if not thresholds:
            continue

        user = db.query(User).filter(User.id == user_id).first()
        if not user or not user.tg_id:
            continue
        # Scan without holding authority locks across Telegram I/O.
        if not user.is_active or not db.query(access_predicate(user.id, delivery=True)).scalar():
            continue

        today = _now_in_user_tz(user).date()
        total_days = int(total_days_hint or 1)
        active_day = derive_current_day(db, plan_id, total_days)

        if active_day not in thresholds:
            continue

        already_sent = (
            db.query(UserEvent)
            .filter(
                UserEvent.user_id == user_id,
                UserEvent.event_name == "pulse_sent",
                UserEvent.plan_id == plan_id,
                UserEvent.properties["active_day"].astext == str(active_day),
            )
            .first()
        )
        if already_sent:
            continue

        try:
            token = issue_report_grant(db, user.id, plan_id, 'pulse_report')
            db.commit()
        except AccessDenied:
            # Access can change after the nonlocking scan. Skip this owner,
            # release its locks, and preserve delivery for the remaining plans.
            db.rollback()
            continue
        url = f"{settings.APP_BASE_URL}/pulse/{token}"
        week_num = ceil(active_day / 7)

        text = (
            f"📊 Тиждень {week_num} — snapshot твого плану.\n\n"
            f"Подивись як іде процес. Посилання діє 7 днів:"
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Відкрити", url=url)]]
        )

        try:
            claim_at = await asyncio.to_thread(outbound_claim, user.id, delivery=True)
            if claim_at is None:
                continue
            await bot.send_message(user.tg_id, text, reply_markup=keyboard)
        except Exception:
            continue

        log_user_event(
            db,
            user_id=user_id,
            event_type="pulse_sent",
            event_source="scheduler",
            source_operation_id=f"scheduler:pulse-snapshot:{plan_id}:{active_day}",
            plan_id=plan_id,
            context={"active_day": active_day, "date": str(today)},
            attribution_at=claim_at,
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
_COMPLETION_WORKERS = 2
completion_receipts = BlockingLane(_COMPLETION_WORKERS, 'completion-receipt')
_continuation_workers = ThreadPoolExecutor(max_workers=_COMPLETION_WORKERS, thread_name_prefix='continuation')
_continuation_inflight_lock = Lock()
_continuation_inflight: set[int] = set()
_schedule_workers = ThreadPoolExecutor(max_workers=_COMPLETION_WORKERS, thread_name_prefix='completion-schedule')
_schedule_inflight_lock = Lock()
_schedule_inflight: set[int] = set()
_report_workers = ThreadPoolExecutor(max_workers=_COMPLETION_WORKERS, thread_name_prefix='completion-report')
_report_inflight_lock = Lock()
_report_inflight: set[int] = set()


def _lane_capacity(lock, inflight):
    with lock:
        return max(0, _COMPLETION_WORKERS - len(inflight)), tuple(inflight)


def _submit_lane(executor, lock, inflight, key, work, *args, wake_completion=True):
    with lock:
        if key in inflight or len(inflight) >= _COMPLETION_WORKERS:
            return False
        inflight.add(key)
    try:
        future = executor.submit(work, *args)
    except Exception:
        with lock:
            inflight.discard(key)
        raise

    def finished(result):
        with lock:
            inflight.discard(key)
        try:
            result.result()
        except Exception:
            logger.exception('Completion recovery worker failed key=%s', key)
        if wake_completion:
            try:
                enable_completion_recovery()
            except Exception:
                logger.exception('Completion recovery wake failed key=%s', key)

    future.add_done_callback(finished)
    return True


_schedule_repair_workers = ThreadPoolExecutor(max_workers=_COMPLETION_WORKERS, thread_name_prefix="schedule-repair")
_schedule_repair_lock = Lock()
_schedule_repair_inflight: set[int] = set()


def dispatch_plan_schedule_recovery():
    """Lease a small number of durable repairs before dispatching by owner."""
    capacity, inflight = _lane_capacity(_schedule_repair_lock, _schedule_repair_inflight)
    if not capacity:
        return
    now = datetime.now(timezone.utc)
    claimed = []
    owners = set(inflight)
    with SessionLocal() as db:
        candidates = (db.query(AIPlan).filter(AIPlan.schedule_reconcile_at <= now,
            ~AIPlan.user_id.in_(inflight)).order_by(AIPlan.schedule_reconcile_at, AIPlan.id)
            .limit(capacity * 4).with_for_update(skip_locked=True).all())
        for plan in candidates:
            if plan.user_id in owners:
                continue
            owners.add(plan.user_id)
            plan.schedule_reconcile_at = now + timedelta(seconds=15)
            claimed.append((plan.user_id, plan.id, plan.schedule_switch_source, plan.total_days))
            if len(claimed) == capacity:
                break
        if claimed:
            db.commit()
    for user_id, plan_id, switch_source, total_days in claimed:
        _submit_lane(_schedule_repair_workers, _schedule_repair_lock, _schedule_repair_inflight,
                     user_id, _repair_plan_schedule, user_id, plan_id, switch_source, total_days,
                     wake_completion=False)


def _repair_plan_schedule(user_id, plan_id, switch_source, total_days):
    if switch_source is None:
        reconcile_plan_schedule(plan_id, skip_busy_owner=True)
        return

    from app.lifecycle import find_lifecycle_operation, switch_plan_format
    from app.plan_runtime.tools import _finish_plan_format_result

    with SessionLocal() as db:
        user = db.query(User).filter_by(id=user_id).with_for_update(skip_locked=True).first()
        if user is None or not user.is_active or not may_access(db, user.id):
            return
        request = find_lifecycle_operation(db, user_id, switch_source)
        if request is None or request.operation != "switch_plan_format":
            logger.warning("Unreplayable switch schedule repair plan=%s", plan_id)
            return
        replay = switch_plan_format(
            db, user_id=user_id, target_plan_type="SHORT" if total_days == 7 else "MEDIUM",
            source_operation_id=switch_source,
        )
    outcome = _finish_plan_format_result(replay, pending_status="needs_evening_time")
    if outcome.get("code") == "superseded":
        with SessionLocal.begin() as db:
            db.query(AIPlan).filter(AIPlan.id == plan_id, AIPlan.schedule_switch_source == switch_source).update({
                AIPlan.schedule_switch_source: None, AIPlan.schedule_reconcile_at: None,
            }, synchronize_session=False)


def _continuation_capacity() -> tuple[int, tuple[int, ...]]:
    return _lane_capacity(_continuation_inflight_lock, _continuation_inflight)


def _submit_continuation(user_id: int, plan_id: int) -> bool:
    return _submit_lane(_continuation_workers, _continuation_inflight_lock,
                        _continuation_inflight, plan_id, request_plan_completion, user_id, plan_id)


def _dispatch_completion_report(user_id, plan_id):
    from app.orchestrator import send_plan_completion_message
    coro = send_plan_completion_message(user_id, plan_id)
    future = _submit_coroutine(coro)
    if future is None:
        coro.close()
        return
    try:
        future.result()
    except Exception:
        logger.exception('Completion dispatch unconfirmed plan=%s', plan_id)


def _terminal_completion_sources(db, *, now=None):
    from app.db import PlanContinuationAttempt
    from app.lifecycle import TERMINAL_STEP_STATUSES
    child = db.query(AIPlanStep.id).join(AIPlanDay).filter(AIPlanDay.plan_id == AIPlan.id)
    last_action = (child.with_entities(AIPlanStep.step_status)
        .filter(AIPlanStep.terminal_at.isnot(None))
        .order_by(AIPlanStep.terminal_at.desc(), AIPlanStep.id.desc())
        .limit(1).correlate(AIPlan).scalar_subquery())
    sources = db.query(AIPlan.user_id, AIPlan.id).join(User).filter(
        or_(AIPlan.status == 'active', and_(AIPlan.status == 'paused', last_action.in_(['completed','skipped']))),
        and_(User.is_active.is_(True), access_predicate(User.id, delivery=True)), child.exists(),
        ~child.filter(AIPlanStep.step_status.notin_(TERMINAL_STEP_STATUSES)).exists(),
        ~child.filter(AIPlanStep.terminal_at.is_(None), AIPlanStep.version != 0).exists(),
    )
    if now is not None:
        sources = sources.filter(~db.query(PlanContinuationAttempt.source_plan_id).filter(
            PlanContinuationAttempt.source_plan_id == AIPlan.id,
            PlanContinuationAttempt.retry_at > now,
        ).exists())
    return sources


def _completion_recovery_time(now):
    from app.db import PlanCompletion, PlanContinuationAttempt
    with SessionLocal() as db:
        accessible = and_(User.is_active.is_(True), access_predicate(AIPlan.user_id, delivery=True))
        obligations = db.query(PlanCompletion).join(AIPlan, AIPlan.id == PlanCompletion.plan_id).join(User, User.id == AIPlan.user_id)
        in_flight = PlanCompletion.state.in_(['in_flight', 'release_in_flight'])
        report_capacity, reporting = _lane_capacity(_report_inflight_lock, _report_inflight)
        schedule_capacity, scheduling = _lane_capacity(_schedule_inflight_lock, _schedule_inflight)
        wake = (obligations.with_entities(func.min(PlanCompletion.wake_at)).filter(
            or_(in_flight, and_(accessible, PlanCompletion.state == 'pending')),
            ~PlanCompletion.plan_id.in_(reporting),
        ).scalar() if report_capacity else None)
        schedule_wake = (obligations.with_entities(
            func.min(func.coalesce(PlanCompletion.schedule_wake_at, now)),
        ).filter(accessible, PlanCompletion.schedule_pending.is_(True),
                 ~PlanCompletion.plan_id.in_(scheduling)).scalar() if schedule_capacity else None)
        sources = _terminal_completion_sources(db).subquery()
        continuation_wake = db.query(func.min(PlanContinuationAttempt.retry_at)).join(
            sources, sources.c.id == PlanContinuationAttempt.source_plan_id,
        ).scalar()
        capacity, inflight = _continuation_capacity()
        ready_sources = capacity > 0 and _terminal_completion_sources(db, now=now).filter(
            ~AIPlan.id.in_(inflight)
        ).limit(1).first() is not None
        deadlines = [_to_utc(value) for value in (wake, schedule_wake, continuation_wake) if value is not None]
        if ready_sources:
            deadlines.append(now + timedelta(seconds=1))
        wake = min(deadlines, default=None)
    if _completion_source_failure:
        retry = now + timedelta(minutes=5)
        wake = min(_to_utc(wake), retry) if wake else retry
        wake = max(now + timedelta(seconds=30), wake)
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


def flag_completion_recovery():
    global _completion_source_failure
    _completion_source_failure = True
    enable_completion_recovery()


def request_plan_completion(user_id, plan_id):
    """Post-commit action/expiry entrance; failures retain terminal source facts."""
    global _completion_source_failure
    from app.orchestrator import _auto_complete_plan_if_needed
    from app.plan_completion.continuation import prepare_automatic_continuation
    try:
        result = prepare_automatic_continuation(
            SessionLocal, user_id, _auto_complete_plan_if_needed, expected_plan_id=plan_id,
        )
        if result is not None:
            # Normal completion is fast, but no-action waits for the durable due
            # time. claim_report is the sole arbiter even when called early.
            _submit_lane(_report_workers, _report_inflight_lock, _report_inflight,
                         plan_id, _dispatch_completion_report, user_id, plan_id)
    except Exception:
        _completion_source_failure = True
        logger.exception('Explicit completion pending user=%s plan=%s', user_id, plan_id)
    finally:
        try:
            enable_completion_recovery()
        except Exception:
            logger.exception("Continuation recovery wake failed plan=%s", plan_id)


def reconcile_plan_completions():
    """Safety Car: unfinished sources/obligations only, no repaired user sessions."""
    global _completion_source_failure
    from app.db import PlanCompletion
    now = datetime.now(timezone.utc)
    try:
        # Terminal source scan is narrow SQL and opens a session only per actual
        # unfinished source, never per historical completed plan.
        capacity, inflight = _continuation_capacity()
        with SessionLocal() as db:
            sources = (_terminal_completion_sources(db, now=now)
                .filter(~AIPlan.id.in_(inflight)).order_by(AIPlan.id).limit(capacity).all()) if capacity else []
        _completion_source_failure = False
        for user_id, plan_id in sources:
            _submit_continuation(user_id, plan_id)
        now = datetime.now(timezone.utc)
        report_capacity, reporting = _lane_capacity(_report_inflight_lock, _report_inflight)
        schedule_capacity, scheduling = _lane_capacity(_schedule_inflight_lock, _schedule_inflight)
        with SessionLocal() as db:
            due_query = db.query(PlanCompletion.plan_id, AIPlan.user_id).join(
                AIPlan, AIPlan.id == PlanCompletion.plan_id).join(User, User.id == AIPlan.user_id).filter(
                    PlanCompletion.state.in_(['pending', 'in_flight', 'release_in_flight']),
                PlanCompletion.wake_at <= now, or_(and_(User.is_active.is_(True), access_predicate(AIPlan.user_id, delivery=True)), PlanCompletion.state.in_(['in_flight','release_in_flight'])),
                ~PlanCompletion.plan_id.in_(reporting))
            due = due_query.order_by(PlanCompletion.wake_at, PlanCompletion.plan_id).limit(report_capacity).all() if report_capacity else []
            schedule_query = db.query(PlanCompletion.plan_id, PlanCompletion.successor_plan_id).join(AIPlan, AIPlan.id == PlanCompletion.plan_id).join(User, User.id == AIPlan.user_id).filter(User.is_active.is_(True), access_predicate(AIPlan.user_id, delivery=True)).filter(
                PlanCompletion.schedule_pending.is_(True),
                func.coalesce(PlanCompletion.schedule_wake_at, now) <= now,
                ~PlanCompletion.plan_id.in_(scheduling))
            schedules = schedule_query.order_by(PlanCompletion.plan_id).limit(schedule_capacity).all() if schedule_capacity else []
        for source_id, successor_id in schedules:
            try:
                _submit_lane(_schedule_workers, _schedule_inflight_lock, _schedule_inflight,
                             source_id, reconcile_plan_schedule, successor_id)
            except Exception:
                logger.exception('Completion schedule interrupted source=%s', source_id)
                _completion_source_failure = True
        for plan_id, user_id in due:
            _submit_lane(_report_workers, _report_inflight_lock, _report_inflight,
                         plan_id, _dispatch_completion_report, user_id, plan_id)
    except Exception:
        logger.exception('Completion recovery interrupted')
        _completion_source_failure = True
    finally:
        enable_completion_recovery()


def cancel_privacy_jobs(user_id, plan_ids):
    """Remove this subject's jobs; store errors leave erasure pending."""
    from apscheduler.jobstores.base import JobLookupError
    with SessionLocal() as db:
        step_ids = {r[0] for r in db.query(AIPlanStep.id).join(AIPlanDay).filter(
            AIPlanDay.plan_id.in_(plan_ids))}
    prefixes = tuple(f'plan_{plan_id}_' for plan_id in plan_ids)
    completion_ids = {f'{prefix}{plan_id}' for plan_id in plan_ids for prefix in
        ('completion_','completion_retry_','completion_receipt_retry_')}
    def owned(job):
        args = getattr(job, 'args', ())
        step_job = (getattr(job, 'func_ref', None) == 'app.scheduler:send_scheduled_message'
            and len(args) == 3 and args[2] in step_ids)
        return (prefixes and job.id.startswith(prefixes)) or job.id in completion_ids or step_job
    for job in scheduler.get_jobs():
        if owned(job):
            try:
                scheduler.remove_job(job.id)
            except JobLookupError:
                pass  # A concurrent remover already confirmed this absence.
    if any(owned(job) for job in scheduler.get_jobs()):
        raise RuntimeError('personal_jobs_remain')


def _cancel_privacy_job_page(user_id, _plan_ids, after):
    """Scan one bounded page, including historical jobs with arbitrary IDs."""
    import re

    from apscheduler.jobstores.base import JobLookupError

    if scheduler is not _application_scheduler:
        cancel_privacy_jobs(user_id, _plan_ids)
        return None

    jobs, cursor = jobstores["default"].page(after, 128)
    plan_by_job = {}
    step_by_job = {}
    for job in jobs:
        match = re.fullmatch(r"plan_([0-9]+)_.*|completion_(?:receipt_retry_|retry_)?([0-9]+)", job.id)
        if match:
            # Persisted plans/steps are positive PostgreSQL INTEGER keys.
            plan_id = int(match[1] or match[2])
            if 0 < plan_id < 2**31:
                plan_by_job[job.id] = plan_id
        if job.func_ref == "app.scheduler:send_scheduled_message" and len(job.args) == 3:
            step_id = job.args[2]
            if type(step_id) is int and 0 < step_id < 2**31:
                step_by_job[job.id] = step_id

    with SessionLocal() as db:
        plans = {plan_id for plan_id, in db.query(AIPlan.id).filter(
            AIPlan.user_id == user_id, AIPlan.id.in_(plan_by_job.values()))}
        steps = {step_id for step_id, in db.query(AIPlanStep.id).join(AIPlanDay).join(AIPlan).filter(
            AIPlan.user_id == user_id, AIPlanStep.id.in_(step_by_job.values()))}
    for job in jobs:
        if plan_by_job.get(job.id) in plans or step_by_job.get(job.id) in steps:
            try:
                scheduler.remove_job(job.id, jobstore="default")
            except JobLookupError:
                pass

    return cursor


_privacy_db = BlockingLane(2, "privacy-db")
_privacy_maintenance_running = asyncio.Lock()
_PRIVACY_JOB_PAGES_PER_PASS = 16


def _privacy_maintenance_batch(*, followup):
    from app.access_proof import purge_expired_proofs
    from app.privacy import purge_retained_data
    from app.db import PrivacyRequest

    if not followup:
        with SessionLocal() as db:
            purge_expired_proofs(db)
            db.commit()
        total = 0
        for _ in range(10):
            with SessionLocal() as db:
                count = purge_retained_data(db)
                db.commit()
            total += count
            if count < 500:
                break
        logger.info("privacy_retention_removed=%d", total)
    with SessionLocal() as db:
        queue = db.query(PrivacyRequest.id).filter_by(state="deleting")
        if followup:
            queue = queue.filter(PrivacyRequest.evidence["job_scan_after"].astext.is_not(None))
        return [r[0] for r in queue.order_by(
            PrivacyRequest.last_attempted_at.asc().nullsfirst(),
            PrivacyRequest.received_at, PrivacyRequest.id).limit(10)]


async def privacy_maintenance(*, followup=False):
    """Two concurrent erasures, durably checkpointing each bounded page."""
    from app.privacy import delete_account
    from app.session_memory import SessionMemory

    if _privacy_maintenance_running.locked():
        return False
    async with _privacy_maintenance_running:
        pending = await _privacy_db.run(_privacy_maintenance_batch, followup=followup)
        cases = asyncio.Semaphore(2)

        async def erase(request_id):
            async with cases:
                try:
                    return await delete_account(
                        request_id, memory=SessionMemory(), cancel_jobs=cancel_privacy_jobs,
                        run_blocking=_privacy_db.run, cancel_job_page=_cancel_privacy_job_page,
                        job_scan_page_limit=_PRIVACY_JOB_PAGES_PER_PASS,
                    )
                except Exception as exc:
                    logger.error("privacy_erasure_pending error_type=%s", type(exc).__name__)
                    return False

        return any(await asyncio.gather(*(erase(request_id) for request_id in pending)))


def run_privacy_maintenance(*, followup=False):
    future = _submit_coroutine(privacy_maintenance(followup=followup))
    if future is not None and future.result(timeout=120):
        scheduler.add_job(
            "app.scheduler:run_privacy_maintenance", "date",
            id="privacy_scan_followup", jobstore="maintenance",
            run_date=datetime.now(timezone.utc) + timedelta(seconds=1),
            kwargs={"followup": True}, replace_existing=True, misfire_grace_time=None,
        )
