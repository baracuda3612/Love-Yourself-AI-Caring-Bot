import asyncio
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import logging

import pytz
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import (
    AIPlan,
    AIPlanDay,
    AIPlanStep,
    ChatHistory,
    ContentLibrary,
    PlanInstance,
    SessionLocal,
    User,
    UserEvent,
    UserProfile,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from app.logging.router_logging import log_metric
from app.plan_adaptations import PlanAdaptationError, apply_plan_adaptation
from app.scheduler import cancel_plan_step_jobs, reschedule_plan_steps
from app.redis_client import redis_client
from app.session_memory import SessionMemory
from app.time_slots import compute_scheduled_for, resolve_daily_time_slots
from app.ux.persona import get_persona
from app.ux.plan_messages import build_activation_info_message
from app.plan_drafts.service import create_plan
from app.plan_finalization import (
    ActivePlanExistsError,
    DraftNotFoundError,
    FinalizationError,
    InvalidDraftError,
    activate_plan_side_effects,
    finalize_plan,
    validate_for_finalization,
)
from app.workers.coach_agent import _build_idle_finished_context, coach_agent
from app.fsm.guards import can_transition
from app.fsm.states import (
    FSM_ALLOWED_STATES,
    IDLE_STATES,
    PLAN_CREATION_ENTRY_STATES,
    SCHEDULE_ADJUSTMENT,
)
from app.workers.mock_workers import (
    mock_onboarding_agent,
    mock_safety_agent,
)
from app.schemas.planner import DifficultyLevel, GeneratedPlan, StepType
from app.plan_duration import assert_canonical_total_days
from app.telemetry import log_user_event

session_memory = SessionMemory(limit=20)
logger = logging.getLogger(__name__)


class PlanAgentEnvelopeError(ValueError):
    """Raised when a generated plan payload is structurally invalid."""


PLAN_CONTRACT_VERSION = "v1"
PLAN_SCHEMA_VERSION = "v1"
PLAN_GENERATION_WAIT_MESSAGE = "⏳ План генерується…"
PLAN_GENERATION_ERROR_MESSAGE = (
    "⚠️ Не вдалося згенерувати план.\nСпробуй ще раз або зміни параметри."
)
PLAN_FINALIZATION_ERROR_MESSAGE = "⚠️ Не вдалося активувати план."
PLAN_DURATION_VALUES = {"SHORT", "MEDIUM", "STANDARD", "LONG"}
PLAN_LOAD_VALUES = {"LITE", "MID", "INTENSIVE"}

SLOT_RANGES = {
    "DAY": (time(12, 0), time(17, 59)),
    "EVENING": (time(18, 0), time(23, 59)),
}
SLOT_DEFAULT_TIMES = {"DAY": "13:00", "EVENING": "20:00"}


def infer_slot(t: time) -> str | None:
    for slot, (start, end) in SLOT_RANGES.items():
        if start <= t <= end:
            return slot
    return None


def _build_task_select_keyboard(active_tasks: Dict[str, str]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=f"📌 {current_time}", callback_data=f"sched_task:{slot}")]
        for slot, current_time in active_tasks.items()
    ]
    if len(active_tasks) > 1:
        buttons.append([InlineKeyboardButton(text="🔀 Змінити кілька", callback_data="sched_task:MULTI")])
    buttons.append([InlineKeyboardButton(text="❌ Скасувати зміни", callback_data="sched_task:CANCEL")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _build_time_select_keyboard(slot: str, current_time: str, in_multi: bool = False) -> InlineKeyboardMarkup:
    start, end = SLOT_RANGES[slot]
    options: List[InlineKeyboardButton] = []
    t = start
    while t <= end:
        label = t.strftime("%H:%M")
        display = f"• {label}" if label == current_time else label
        options.append(InlineKeyboardButton(text=display, callback_data=f"sched_time:{slot}:{label}"))
        t = (datetime.combine(date.today(), t) + timedelta(minutes=30)).time()

    rows = [options[i:i + 3] for i in range(0, len(options), 3)]
    rows.append([InlineKeyboardButton(text="✏️ Свій варіант", callback_data=f"sched_time:{slot}:CUSTOM")])
    if in_multi:
        rows.append([InlineKeyboardButton(text="✓ Тільки це завдання", callback_data=f"sched_time:{slot}:ONLY_THIS")])
    rows.append([InlineKeyboardButton(text="❌ Скасувати зміни", callback_data=f"sched_time:{slot}:CANCEL")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _get_plan_active_tasks(plan_id: int, current_day: int, user: User, db: Session) -> Dict[str, str]:
    rows = (
        db.query(AIPlanStep.time_slot)
        .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
        .filter(
            AIPlanDay.plan_id == plan_id,
            AIPlanDay.day_number >= current_day,
        )
        .distinct()
        .all()
    )
    slots_in_plan = {row[0] for row in rows}
    daily_times = resolve_daily_time_slots(user.profile)
    return {slot: daily_times.get(slot, SLOT_DEFAULT_TIMES[slot]) for slot in slots_in_plan if slot in SLOT_RANGES}


def _expected_time_slots_for_load(load: str | None) -> int | None:
    if load == "LITE":
        return 1
    if load == "MID":
        return 2
    if load == "INTENSIVE":
        return 3
    return None


def _plan_agent_fallback_envelope() -> Dict[str, Any]:
    return {
        "reply_text": PLAN_GENERATION_ERROR_MESSAGE,
        "tool_call": None,
    }


def _resume_plan_if_paused(db: Session, plan: AIPlan) -> Tuple[bool, List[int]]:
    if plan.status != "paused":
        return False, []
    try:
        result = apply_plan_adaptation(db, plan.id, {"adaptation_type": "resume"})
    except Exception:
        logger.exception("[SCHED_ADJ] Failed to resume paused plan=%s", plan.id)
        return False, []

    resumed = plan.status == "active"
    return resumed, list(result.rescheduled_step_ids or [])




async def _handle_schedule_adjustment_init(user_id: int, tool_args: Dict[str, Any], db: Session) -> Dict[str, Any]:
    user = db.query(User).filter(User.id == user_id).first()
    active_plan = get_active_plan(db, user_id)
    if not active_plan or not user:
        return {"user_text": "Активний план не знайдено."}

    current_day = getattr(active_plan, "current_day", 1) or 1
    active_tasks = _get_plan_active_tasks(active_plan.id, current_day, user, db)
    if not active_tasks:
        return {"user_text": "Немає майбутніх завдань для зміни часу."}

    first_slot = list(active_tasks.keys())[0]
    is_single = len(active_tasks) == 1
    is_paused = user.current_state == "ACTIVE_PAUSED"

    await _commit_fsm_transition(
        user_id=user_id,
        agent="plan",
        next_state=SCHEDULE_ADJUSTMENT,
        db=db,
        reason="schedule_adjustment_initiated",
    )

    ctx = {
        "active_tasks": active_tasks,
        "slots_queue": [] if is_single else list(active_tasks.keys()),
        "current_slot": first_slot,
        "pending_changes": {},
        "step": "time_select" if is_single else "task_select",
        "plan_was_paused": is_paused,
    }
    await session_memory.set_schedule_adjustment_context(user_id, ctx)
    await session_memory.set_schedule_adjustment_last_active(user_id)

    keyboard = _build_time_select_keyboard(first_slot, active_tasks[first_slot], in_multi=False) if is_single else _build_task_select_keyboard(active_tasks)
    return {"user_text": tool_args.get("user_text", ""), "keyboard": keyboard}


async def _handle_schedule_adjustment_record(user_id: int, tool_args: Dict[str, Any], db: Session) -> Dict[str, Any]:
    import re

    new_time_str = str(tool_args.get("new_time", "")).strip()
    user_text = str(tool_args.get("user_text", ""))

    if not re.match(r"^\d{1,2}:\d{2}$", new_time_str):
        return {"user_text": f"Не можу розпізнати час «{new_time_str}». Введи у форматі ГГ:ХХ."}

    h, m = [int(x) for x in new_time_str.split(":", 1)]
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return {"user_text": "Невалідний час."}

    inferred = infer_slot(time(h, m))
    if inferred is None:
        return {"user_text": "Час має бути між 06:00 і 23:59."}

    ctx = await session_memory.get_schedule_adjustment_context(user_id) or {}
    active_tasks = ctx.get("active_tasks", {})
    slot_being_edited = ctx.get("current_slot")

    if inferred != slot_being_edited:
        slot_start, slot_end = SLOT_RANGES[slot_being_edited]
        range_str = f"{slot_start.strftime('%H:%M')}–{slot_end.strftime('%H:%M')}"
        return {
            "user_text": (
                "Цей час виходить за межі поточного завдання. "
                f"Один слот — одне завдання, тому час має бути між {range_str}. "
                "Спробуй ще раз."
            )
        }

    pending = ctx.get("pending_changes", {})
    pending[slot_being_edited] = {"new_time": new_time_str, "new_slot": slot_being_edited}

    active_tasks[slot_being_edited] = new_time_str

    queue = [s for s in ctx.get("slots_queue", []) if s != slot_being_edited]
    next_slot = queue[0] if queue else None

    await session_memory.update_schedule_adjustment_context(
        user_id,
        {
            "active_tasks": active_tasks,
            "pending_changes": pending,
            "slots_queue": queue,
            "current_slot": next_slot,
            "step": "time_select" if next_slot else "awaiting_apply",
        },
    )
    await session_memory.set_schedule_adjustment_last_active(user_id)

    if next_slot:
        keyboard = _build_time_select_keyboard(next_slot, active_tasks.get(next_slot, SLOT_DEFAULT_TIMES[next_slot]), in_multi=True)
        return {"user_text": user_text, "keyboard": keyboard}
    return {"user_text": user_text}


async def _handle_schedule_adjustment_apply(user_id: int, tool_args: Dict[str, Any], db: Session) -> Dict[str, Any]:
    ctx = await session_memory.get_schedule_adjustment_context(user_id) or {}
    plan_was_paused = bool(ctx.get("plan_was_paused", False))

    user = db.query(User).filter(User.id == user_id).first()
    active_plan = get_active_plan(db, user_id)
    if not user or not active_plan:
        return_state = "ACTIVE_PAUSED" if plan_was_paused else "ACTIVE"
        await _commit_fsm_transition(
            user_id=user_id,
            agent="plan",
            next_state=return_state,
            db=db,
            reason="no_plan",
        )
        return {"user_text": "Активний план не знайдено."}

    pending_changes = ctx.get("pending_changes", {})
    if not pending_changes:
        return_state = "ACTIVE_PAUSED" if plan_was_paused else "ACTIVE"
        await _commit_fsm_transition(
            user_id=user_id,
            agent="plan",
            next_state=return_state,
            db=db,
            reason="no_changes",
        )
        await session_memory.clear_schedule_adjustment_context(user_id)
        return {"user_text": tool_args.get("user_text", "Нічого не змінилось.")}

    current_day = getattr(active_plan, "current_day", 1) or 1
    now_utc = datetime.now(timezone.utc)
    daily_time_slots = dict(resolve_daily_time_slots(user.profile))
    telemetry_changes = []
    step_ids_to_reschedule: List[int] = []

    for old_slot, change in pending_changes.items():
        new_time_str = change.get("new_time")
        new_slot = change.get("new_slot")
        if not new_time_str or not new_slot:
            continue

        if new_slot != old_slot:
            logger.error("[SCHED_ADJ] unexpected cross-slot in pending user=%s", user_id)
            continue

        old_time = daily_time_slots.get(old_slot, SLOT_DEFAULT_TIMES.get(old_slot, ""))
        daily_time_slots[new_slot] = new_time_str

        future_steps = (
            db.query(AIPlanStep)
            .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
            .filter(
                AIPlanDay.plan_id == active_plan.id,
                AIPlanDay.day_number >= current_day,
                AIPlanStep.time_slot == old_slot,
                AIPlanStep.scheduled_for > now_utc,
            )
            .all()
        )

        for step in future_steps:
            step.time_slot = new_slot
            step.scheduled_for = compute_scheduled_for(
                plan_start=active_plan.start_date,
                day_number=step.day.day_number,
                time_slot=new_slot,
                timezone_name=user.timezone,
                daily_time_slots=daily_time_slots,
            )
            db.add(step)
            step_ids_to_reschedule.append(step.id)

        telemetry_changes.append(
            {
                "from_slot": old_slot,
                "to_slot": new_slot,
                "from_time": old_time,
                "to_time": new_time_str,
                "affected_steps": len(future_steps),
            }
        )

    profile = user.profile
    if profile is None:
        profile = UserProfile(user_id=user.id)
        db.add(profile)
        user.profile = profile
    profile.daily_time_slots = daily_time_slots
    db.add(user)
    log_user_event(
        db,
        user_id=user_id,
        event_type="schedule_adjustment",
        context={
            "changes": telemetry_changes,
            "total_affected_steps": len(step_ids_to_reschedule),
            "from_day": current_day,
            "plan_id": active_plan.id,
        },
    )
    db.commit()

    if step_ids_to_reschedule:
        try:
            reschedule_plan_steps(step_ids_to_reschedule)
        except Exception:
            logger.exception("[SCHED_ADJ] reschedule failed user=%s", user_id)

    return_state = "ACTIVE_PAUSED" if plan_was_paused else "ACTIVE"
    await _commit_fsm_transition(
        user_id=user_id,
        agent="plan",
        next_state=return_state,
        db=db,
        reason="schedule_adjustment_applied",
    )
    await session_memory.clear_schedule_adjustment_context(user_id)
    await session_memory.clear_schedule_adjustment_last_active(user_id)
    await session_memory.clear_schedule_adjustment_soft_prompted(user_id)

    return {"user_text": tool_args.get("user_text", "Готово ✅")}


async def _handle_schedule_adjustment_cancel(user_id: int, tool_args: Dict[str, Any], db: Session) -> Dict[str, Any]:
    ctx = await session_memory.get_schedule_adjustment_context(user_id) or {}
    plan_was_paused = bool(ctx.get("plan_was_paused", False))
    return_state = "ACTIVE_PAUSED" if plan_was_paused else "ACTIVE"

    await _commit_fsm_transition(
        user_id=user_id,
        agent="plan",
        next_state=return_state,
        db=db,
        reason="schedule_adjustment_cancelled",
    )
    await session_memory.clear_schedule_adjustment_context(user_id)
    await session_memory.clear_schedule_adjustment_last_active(user_id)
    await session_memory.clear_schedule_adjustment_soft_prompted(user_id)
    return {"user_text": tool_args.get("user_text", "Добре, залишаємо як є.")}


async def run_plan_tool_call(tool_call: Dict[str, Any]) -> Dict[str, Any]:
    """Backward-compatible handler for plan tool call payloads."""
    tool_name = str(tool_call.get("name") or "")
    tool_args = tool_call.get("arguments") if isinstance(tool_call.get("arguments"), dict) else {}
    user_id = int(tool_call.get("user_id") or 0)

    if tool_name == "schedule_adjustment_init" and user_id:
        with SessionLocal() as db:
            return await _handle_schedule_adjustment_init(user_id, tool_args, db)
    if tool_name == "schedule_adjustment_record" and user_id:
        with SessionLocal() as db:
            return await _handle_schedule_adjustment_record(user_id, tool_args, db)
    if tool_name == "schedule_adjustment_apply" and user_id:
        with SessionLocal() as db:
            return await _handle_schedule_adjustment_apply(user_id, tool_args, db)
    if tool_name == "schedule_adjustment_cancel" and user_id:
        with SessionLocal() as db:
            return await _handle_schedule_adjustment_cancel(user_id, tool_args, db)

    if tool_name == "start_plan":
        return {"user_text": "Starting a plan. Tell me what you'd like to plan."}
    return {"user_text": ""}




def _normalize_fsm_state(raw_state: Optional[str]) -> Optional[str]:
    if not raw_state:
        return None
    if not isinstance(raw_state, str):
        return None
    state = raw_state.strip()
    if not state:
        return None
    if ":" in state:
        prefix, suffix = state.split(":", 1)
        prefix = prefix.upper()
        normalized = f"{prefix}:{suffix}"
    else:
        normalized = state.upper()
    if normalized not in FSM_ALLOWED_STATES:
        return None
    return normalized


def _guard_fsm_transition(
    current_state: Optional[str],
    transition_signal: Any,
    agent: str,
    plan_persisted: bool = False,
) -> Tuple[Optional[str], Optional[str]]:
    if transition_signal is None:
        return None, None

    normalized_current = _normalize_fsm_state(current_state) if current_state else None

    normalized_signal = _normalize_fsm_state(transition_signal)
    if normalized_signal is None:
        return None, "invalid_state"

    if normalized_current is None:
        return normalized_signal, None

    if not can_transition(normalized_current, normalized_signal):
        return None, "transition_blocked_by_guards"

    return normalized_signal, None


def _plan_end_date_status(plan_end_date: Optional[datetime]) -> Optional[Tuple[datetime, datetime]]:
    if not plan_end_date:
        return None
    if plan_end_date.tzinfo is None:
        return plan_end_date, datetime.utcnow()
    return plan_end_date.astimezone(pytz.UTC), datetime.now(pytz.UTC)


def _auto_complete_plan_if_needed(db: Session, user: User) -> None:
    if not user.plan_end_date:
        return

    now = datetime.now(timezone.utc)
    plan_end_date = user.plan_end_date
    if plan_end_date.tzinfo is None:
        plan_end_date = plan_end_date.replace(tzinfo=timezone.utc)

    if plan_end_date >= now:
        return

    active_plans = (
        db.query(AIPlan)
        .filter(AIPlan.user_id == user.id, AIPlan.status == "active")
        .order_by(AIPlan.created_at.desc())
        .limit(2)
        .all()
    )

    if len(active_plans) > 1:
        logger.warning(
            "[COMPLETION] Multiple active plans found for user %s; completing latest plan id=%s",
            user.id,
            active_plans[0].id,
        )

    plan = active_plans[0] if active_plans else None

    # IDEMPOTENCY GUARD
    if plan is None or plan.status == "completed":
        if user.current_state not in IDLE_STATES:
            user.current_state = "IDLE_FINISHED"
        user.plan_end_date = None
        db.add(user)
        return

    plan.status = "completed"
    plan.end_date = now
    db.add(plan)

    user.current_state = "IDLE_FINISHED"
    user.plan_end_date = None
    db.add(user)

    completion_rate = None
    adaptation_count = 0
    metrics_error = False
    try:
        from app.plan_metrics import get_completion_rate

        completion_rate = get_completion_rate(db, user.id, plan.id)
    except Exception as e:
        metrics_error = True
        logger.warning("[COMPLETION] metrics failed user=%s plan=%s: %s", user.id, plan.id, e)

    try:
        log_user_event(
            db=db,
            user_id=user.id,
            event_type="plan_completed",
            context={
                "plan_id": plan.id,
                "total_days": plan.total_days,
                "focus": plan.focus,
                "load": plan.load,
                "duration": plan.duration,
                "completion_rate": round(completion_rate, 4) if completion_rate is not None else None,
                "adaptation_count": adaptation_count,
                "metrics_error": metrics_error,
            },
        )
    except Exception as e:
        logger.error("[COMPLETION] log event failed user=%s: %s", user.id, e)
        db.rollback()
        plan.status = "completed"
        plan.end_date = now
        user.current_state = "IDLE_FINISHED"
        user.plan_end_date = None
        db.add(plan)
        db.add(user)

    try:
        asyncio.get_running_loop()
        asyncio.create_task(send_plan_completion_message(user.id, plan.id))
    except RuntimeError:
        logger.warning(
            "[COMPLETION] No running event loop, skipping message task user=%s",
            user.id,
        )


async def send_plan_completion_message(user_id: int, plan_id: int) -> None:
    """
    Sends completion report + CTA to user via Telegram.
    Fire-and-forget. Called from _auto_complete_plan_if_needed.
    Exempt from MAX_AUTO_MESSAGES_PER_DAY — this is a lifecycle event.
    """
    from app.plan_completion.metrics import build_completion_metrics
    from app.plan_completion.report import build_completion_report
    from app.plan_completion.cta import get_next_plan_recommendation
    from app.plan_completion.tokens import make_report_token
    from app.scheduler import _send_message_async

    with SessionLocal() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user or not user.tg_id:
            logger.warning("[COMPLETION_MSG] user=%s not found or no tg_id", user_id)
            return

        already_sent = db.query(UserEvent).filter(
            UserEvent.user_id == user_id,
            UserEvent.event_type == "plan_completion_sent",
            UserEvent.context["plan_id"].astext == str(plan_id),
        ).first()
        if already_sent:
            logger.info("[COMPLETION_MSG] already sent for plan=%s", plan_id)
            return

        try:
            metrics = build_completion_metrics(db, user_id, plan_id)
        except Exception as e:
            logger.error(
                "[COMPLETION_MSG] metrics failed user=%s plan=%s: %s",
                user_id,
                plan_id,
                e,
            )
            return

        persona = "empath"
        if user.profile:
            persona = get_persona(user.profile)

        report_text = build_completion_report(metrics, persona)
        cta = get_next_plan_recommendation(metrics)
        report_url = (
            f"{settings.APP_BASE_URL}/report/"
            f"{make_report_token(plan_id, settings.REPORT_TOKEN_SECRET)}"
        )
        report_text = report_text + f"\n\n🔗 <a href=\"{report_url}\">Детальний звіт →</a>"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=cta.button1_text,
                callback_data=f"start_plan:{cta.button1_params['duration']}:{cta.button1_params['load']}:{cta.button1_params['focus']}",
            ),
            InlineKeyboardButton(
                text=cta.button2_text,
                callback_data=f"start_plan:{cta.button2_params['duration']}:{cta.button2_params['load']}:{cta.button2_params['focus']}",
            ),
        ]])
        tg_id = user.tg_id

    result = await _send_message_async(tg_id, report_text, reply_markup=keyboard)

    if result:
        with SessionLocal() as db:
            log_user_event(
                db,
                user_id=user_id,
                event_type="plan_completion_sent",
                context={"plan_id": plan_id, "outcome_tier": metrics.outcome_tier},
            )
            db.commit()
        return

    _schedule_completion_retry(user_id, plan_id)


def _schedule_completion_retry(user_id: int, plan_id: int) -> None:
    from app.scheduler import scheduler

    scheduler.add_job(
        "app.orchestrator:_retry_completion_message",
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(minutes=30),
        args=[user_id, plan_id],
        id=f"completion_retry_{plan_id}",
        replace_existing=True,
    )
    logger.info("[COMPLETION_MSG] retry scheduled user=%s plan=%s", user_id, plan_id)


def _retry_completion_message(user_id: int, plan_id: int) -> None:
    """APScheduler sync callback — submits async send to event loop."""
    from app.scheduler import _submit_coroutine

    future = _submit_coroutine(send_plan_completion_message(user_id, plan_id))
    if future:
        try:
            future.result(timeout=30)
        except Exception as e:
            logger.error("[COMPLETION_RETRY] failed user=%s: %s", user_id, e)
            _submit_coroutine(_send_failure_notice(user_id))


async def _send_failure_notice(user_id: int) -> None:
    from app.scheduler import _send_message_async

    with SessionLocal() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if user and user.tg_id:
            await _send_message_async(
                user.tg_id,
                "⚠️ Не вдалось надіслати звіт про завершення плану. "
                "Зверніться в підтримку якщо це повторюється.",
            )


def _trigger_plan_completion(user_id: int, plan_id: int) -> None:
    """
    APScheduler sync callback.
    Calls _auto_complete_plan_if_needed and submits send_plan_completion_message.
    """
    from app.scheduler import _submit_coroutine

    with SessionLocal() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return
        _auto_complete_plan_if_needed(db, user)
        try:
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error("[COMPLETION_TRIGGER] db commit failed user=%s: %s", user_id, e)
            return

    future = _submit_coroutine(send_plan_completion_message(user_id, plan_id))
    if future:
        try:
            future.result(timeout=30)
        except Exception as e:
            logger.error("[COMPLETION_TRIGGER] send failed user=%s: %s", user_id, e)


def _auto_complete_plan_if_needed_for_user_id(user_id: int) -> None:
    with SessionLocal() as db:
        user: Optional[User] = db.query(User).filter(User.id == user_id).first()
        if not user:
            return

        _auto_complete_plan_if_needed(db, user)

        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            logger.error(
                "[FSM] Failed to auto-complete plan for user %s (plan_end_date=%s)",
                user_id,
                user.plan_end_date,
            )


def _auto_drop_plan_for_new_flow(user_id: int) -> bool:
    with SessionLocal() as db:
        user: Optional[User] = db.query(User).filter(User.id == user_id).first()
        if not user:
            return False
        if user.current_state not in {"ACTIVE", "ACTIVE_PAUSED"}:
            return False

        active_plan = (
            db.query(AIPlan)
            .filter(AIPlan.user_id == user_id, AIPlan.status == "active")
            .order_by(AIPlan.created_at.desc())
            .first()
        )

        step_ids: List[int] = []
        if active_plan:
            step_rows = (
                db.query(AIPlanStep.id)
                .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
                .filter(AIPlanDay.plan_id == active_plan.id)
                .all()
            )
            step_ids = [row[0] for row in step_rows]
            active_plan.status = "abandoned"
            active_plan.end_date = datetime.now(timezone.utc)

        user.current_state = "IDLE_DROPPED"
        user.plan_end_date = None

        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            logger.error(
                "[FSM] Failed to auto-drop plan for user %s",
                user_id,
            )
            return False

    if step_ids:
        cancel_plan_step_jobs(step_ids)
    logger.info(
        "[FSM] Auto-dropped plan before new plan flow for user %s",
        user_id,
    )
    return True


async def _commit_fsm_transition(
    user_id: int,
    agent: str,
    next_state: str,
    db: Optional[Session] = None,
    reason: str = "",
) -> Optional[str]:
    """Commit FSM transition with guard validation.

    If ``db`` is provided, transition is staged into that session and caller controls commit.
    Otherwise function opens and commits its own session for backward compatibility.
    """

    def _apply_transition(session: Session) -> Optional[str]:
        user: Optional[User] = session.query(User).filter(User.id == user_id).first()
        if not user:
            raise ValueError(f"User {user_id} not found")

        previous_state = user.current_state
        if previous_state == next_state:
            logger.debug(
                "[FSM] No-op transition for user %s already in %s (agent=%s)",
                user_id,
                next_state,
                agent,
            )
            return previous_state

        if not can_transition(previous_state, next_state):
            raise ValueError(
                f"Transition {previous_state} → {next_state} not allowed by FSM guards"
            )

        user.current_state = next_state
        session.add(user)
        logger.info(
            "[FSM] User %s state transition: %s → %s (agent=%s, reason=%s)",
            user_id,
            previous_state,
            next_state,
            agent,
            reason,
        )
        log_metric(
            "fsm_transition",
            extra={
                "user_id": user_id,
                "agent": agent,
                "from_state": previous_state,
                "to_state": next_state,
                "reason": reason,
            },
        )
        return previous_state

    if db is not None:
        previous_state = _apply_transition(db)
        return previous_state

    with SessionLocal() as managed_db:
        previous_state = _apply_transition(managed_db)
        managed_db.commit()

    return previous_state


def _safe_timezone(name: Optional[str]) -> pytz.BaseTzInfo:
    try:
        return pytz.timezone(name or "Europe/Kyiv")
    except pytz.UnknownTimeZoneError:
        return pytz.timezone("Europe/Kyiv")


def _derive_plan_end_date(plan: GeneratedPlan, tz: pytz.BaseTzInfo) -> Optional[datetime]:
    duration_days = plan.duration_days or len(plan.schedule)
    if duration_days <= 0:
        return None
    now_local = datetime.now(tz)
    end_local = now_local + timedelta(days=duration_days)
    return end_local.astimezone(pytz.UTC)


def _extract_exercise_ids(plan_payload: Dict[str, Any]) -> List[str]:
    exercise_ids: List[str] = []
    schedule = plan_payload.get("schedule")
    if not isinstance(schedule, list):
        return exercise_ids
    for day in schedule:
        if not isinstance(day, dict):
            continue
        steps = day.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            exercise_id = step.get("exercise_id")
            if exercise_id:
                exercise_ids.append(str(exercise_id))
    return exercise_ids


def _load_plan_exercise_ids(db: Session, plan_id: int) -> List[str]:
    rows = (
        db.query(AIPlanStep.exercise_id)
        .join(AIPlanDay, AIPlanStep.day_id == AIPlanDay.id)
        .filter(AIPlanDay.plan_id == plan_id, AIPlanStep.exercise_id.isnot(None))
        .all()
    )
    return [row[0] for row in rows if row[0]]


def _validate_plan_exercise_ids(
    db: Session,
    user: User,
    plan_payload: Dict[str, Any],
    latest_plan: Optional[AIPlan],
) -> None:
    new_exercise_ids = set(_extract_exercise_ids(plan_payload))
    if not new_exercise_ids:
        return
    known_ids = {
        row[0]
        for row in db.query(ContentLibrary.id)
        .filter(ContentLibrary.id.in_(new_exercise_ids))
        .all()
    }
    if new_exercise_ids - known_ids:
        raise PlanAgentEnvelopeError("invalid_exercise_ids")
    if latest_plan and latest_plan.status == "active":
        previous_ids = set(_load_plan_exercise_ids(db, latest_plan.id))
        if new_exercise_ids - previous_ids:
            raise PlanAgentEnvelopeError("new_exercise_ids_not_allowed")


def _persist_generated_plan(db: Session, user: User, plan_payload: Dict[str, Any]) -> AIPlan:
    try:
        parsed_plan = GeneratedPlan.parse_obj(plan_payload)
    except ValidationError as exc:
        raise PlanAgentEnvelopeError("invalid_generated_plan_object") from exc

    latest_plan = (
        db.query(AIPlan)
        .filter(AIPlan.user_id == user.id)
        .order_by(AIPlan.created_at.desc())
        .first()
    )
    _validate_plan_exercise_ids(db, user, plan_payload, latest_plan)
    if latest_plan and latest_plan.status == "active":
        latest_plan.status = "abandoned"

    plan_load = plan_payload.get("load")
    if not plan_load:
        logger.error("Attempted to activate plan without load")
        raise RuntimeError("Active plan must have non-null load")
    normalized_plan_load = str(plan_load).strip().upper()
    if normalized_plan_load not in PLAN_LOAD_VALUES:
        logger.error("Attempted to activate plan without load")
        raise RuntimeError("Active plan must have non-null load")

    try:
        assert_canonical_total_days(parsed_plan.duration_days)
    except ValueError as exc:
        raise PlanAgentEnvelopeError("invalid_generated_plan_duration") from exc

    plan_start = datetime.now(timezone.utc)
    ai_plan = AIPlan(
        user_id=user.id,
        title=parsed_plan.title,
        module_id=parsed_plan.module_id,
        goal_description=parsed_plan.reasoning,
        status="active",
        load=normalized_plan_load,
        start_date=plan_start,
        total_days=parsed_plan.duration_days,
    )
    db.add(ai_plan)
    db.flush()

    logger.info(
        "Plan %s activated with load=%s for user %s",
        ai_plan.id,
        ai_plan.load,
        user.id,
    )

    daily_time_slots = resolve_daily_time_slots(user.profile)

    for day in parsed_plan.schedule:
        day_record = AIPlanDay(
            plan_id=ai_plan.id,
            day_number=day.day_number,
            focus_theme=day.focus_theme,
        )
        db.add(day_record)
        db.flush()
        for index, step in enumerate(day.steps):
            scheduled_for = compute_scheduled_for(
                plan_start=plan_start,
                day_number=day.day_number,
                time_slot=step.time_slot,
                timezone_name=user.timezone,
                daily_time_slots=daily_time_slots,
            )
            step_type = step.step_type.value
            assert step_type in {entry.value for entry in StepType}
            difficulty = step.difficulty.value
            assert difficulty in {entry.value for entry in DifficultyLevel}
            db.add(
                AIPlanStep(
                    day_id=day_record.id,
                    exercise_id=step.exercise_id,
                    title=step.title,
                    description=step.description,
                    step_type=step_type,
                    difficulty=difficulty,
                    order_in_day=index,
                    time_slot=step.time_slot,
                    scheduled_for=scheduled_for,
                )
            )

    db.add(
        PlanInstance(
            user_id=user.id,
            blueprint_id=str(parsed_plan.module_id),
            initial_parameters=plan_payload,
            contract_version=str(plan_payload.get("contract_version") or PLAN_CONTRACT_VERSION),
            schema_version=str(plan_payload.get("schema_version") or PLAN_SCHEMA_VERSION),
        )
    )

    tz = _safe_timezone(user.timezone)
    user.plan_end_date = _derive_plan_end_date(parsed_plan, tz)

    log_metric(
        "plan_snapshot",
        extra={
            "timestamp": datetime.utcnow().isoformat(),
            "user_id": user.id,
            "plan_summary": parsed_plan.title,
            "plan_key_parameters": {
                "module_id": str(parsed_plan.module_id),
                "duration_days": parsed_plan.duration_days,
                "schedule_days": len(parsed_plan.schedule),
                "milestones": len(parsed_plan.milestones),
            },
        },
    )
    return ai_plan


async def get_stm_history(user_id: int) -> List[Dict[str, str]]:
    """Short-term memory with Redis primary and Postgres fallback."""

    history = await session_memory.get_recent_messages(user_id)
    if history:
        return [
            {"role": item.get("role"), "content": item.get("text")}
            for item in history
            if isinstance(item, dict)
        ]

    with SessionLocal() as db:
        rows = (
            db.query(ChatHistory.role, ChatHistory.text, ChatHistory.created_at)
            .filter(ChatHistory.user_id == user_id)
            .order_by(ChatHistory.created_at.desc())
            .limit(session_memory.limit)
            .all()
        )

    return [
        {"role": row.role, "content": row.text}
        for row in reversed(rows)
    ]


async def get_ltm_snapshot(user_id: int) -> Dict[str, Any]:
    """Long-term snapshot: поля профілю користувача."""
    with SessionLocal() as db:
        profile: Optional[UserProfile] = (
            db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
        )

        if profile:
            # Access all relationship data while the session is active
            return {
                "main_goal": profile.main_goal,
                "communication_style": profile.communication_style,
                "name_preference": profile.name_preference,
                "timezone": profile.user.timezone if profile.user else None,
            }

    return {}


async def get_temporal_context(user_id: int) -> Optional[str]:
    with SessionLocal() as db:
        user: Optional[User] = db.query(User).filter(User.id == user_id).first()

    if not user:
        return None

    tz = _safe_timezone(user.timezone)
    localized_now = datetime.now(tz)
    hour = localized_now.hour

    if 5 <= hour < 12:
        period = "Morning"
    elif 12 <= hour < 17:
        period = "Afternoon"
    elif 17 <= hour < 22:
        period = "Evening"
    else:
        period = "Night"

    return f"{localized_now.strftime('%A')}, {localized_now.strftime('%H:%M')} ({period})"


async def get_fsm_state(user_id: int) -> Optional[str]:
    """Повертає поточний FSM-стан користувача."""
    with SessionLocal() as db:
        user: Optional[User] = db.query(User).filter(User.id == user_id).first()

    return user.current_state if user else None


async def build_user_context(user_id: int, message_text: str) -> Dict[str, Any]:
    stm_history = await get_stm_history(user_id)
    ltm_snapshot = await get_ltm_snapshot(user_id)
    fsm_state = await get_fsm_state(user_id)
    temporal_context = await get_temporal_context(user_id)

    schedule_adjustment_context = await session_memory.get_schedule_adjustment_context(user_id)

    return {
        "message_text": message_text,
        "short_term_history": stm_history,
        "profile_snapshot": ltm_snapshot,
        "current_state": fsm_state,
        "temporal_context": temporal_context,
        "schedule_adjustment_context": schedule_adjustment_context,
    }






def get_active_plan(db: Session, user_id: int) -> Optional[AIPlan]:
    return (
        db.query(AIPlan)
        .filter(AIPlan.user_id == user_id, AIPlan.status.in_(["active", "paused"]))
        .order_by(AIPlan.created_at.desc())
        .first()
    )


def get_daily_task_count(db: Session, plan: AIPlan) -> int:
    first_day = (
        db.query(AIPlanDay)
        .filter(AIPlanDay.plan_id == plan.id)
        .order_by(AIPlanDay.day_number.asc())
        .first()
    )
    if not first_day:
        return 0
    return (
        db.query(AIPlanStep)
        .filter(
            AIPlanStep.day_id == first_day.id,
            AIPlanStep.step_status.notin_(["completed", "skipped", "expired"]),
        )
        .count()
    )


def get_avg_difficulty(db: Session, plan: AIPlan) -> int:
    steps = (
        db.query(AIPlanStep)
        .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
        .filter(AIPlanDay.plan_id == plan.id)
        .all()
    )
    if not steps:
        return 1
    difficulty_map = {
        "EASY": 1,
        "MEDIUM": 2,
        "HARD": 3,
    }

    values = [
        difficulty_map.get(str(step.difficulty).upper(), 1)
        for step in steps
    ]

    return round(sum(values) / len(values))


async def build_plan_draft_preview(
    user_id: int,
    parameters_for_draft: Dict[str, Any],
) -> str:
    # FROZEN T5.2: plan preview removed. Plan is created immediately via create_plan().
    return ""


# Tool name → callable map (allowlist).
# Only these tools may be invoked by Coach via tool_call signal.
_PLAN_TOOL_REGISTRY: Dict[str, Any] = {}


def _build_tool_registry() -> Dict[str, Any]:
    """Lazy-build the tool registry so imports stay at call time."""
    from app.plan_runtime.tools import (
        cancel_plan,
        change_day_time,
        change_evening_time,
        create_first_plan,
        create_followup_plan,
        get_plan_status,
        pause_plan,
        record_evening_time,
        resume_plan,
    )
    return {
        "create_first_plan":    lambda uid, _args: create_first_plan(uid),
        "create_followup_plan": lambda uid, args: create_followup_plan(uid, args.get("plan_type", "SHORT")),
        "record_evening_time":  lambda uid, args: record_evening_time(uid, args["hhmm"]),
        "change_day_time":      lambda uid, args: change_day_time(uid, args["hhmm"]),
        "change_evening_time":  lambda uid, args: change_evening_time(uid, args["hhmm"]),
        "get_plan_status":      lambda uid, _args: get_plan_status(uid),
        "pause_plan":           lambda uid, _args: pause_plan(uid),
        "resume_plan":          lambda uid, _args: resume_plan(uid),
        "cancel_plan":          lambda uid, _args: cancel_plan(uid),
    }


# Deterministic reply templates — no second LLM call needed.
_TOOL_REPLY_TEMPLATES: Dict[str, str] = {
    "create_first_plan":    "✅ Перший 7-денний ритм запущено. Перше завдання прийде в обраний час.",
    "create_followup_plan": "✅ Новий план запущено. Завдання приходитимуть за розкладом.",
    "record_evening_time":  "✅ Вечірній час збережено.",
    "change_day_time":      "✅ Денний час змінено. Наступні завдання прийдуть у новий час.",
    "change_evening_time":  "✅ Вечірній час змінено.",
    "get_plan_status":      None,   # returns dynamic data — formatted below
    "pause_plan":           "⏸ План поставлено на паузу. Завдання не надходитимуть до відновлення.",
    "resume_plan":          "▶️ План відновлено. Завдання повернуться за розкладом.",
    "cancel_plan":          "🛑 План зупинено. Твоя статистика збережена.",
}


async def _execute_plan_tool(user_id: int, tool_call: Dict[str, Any]) -> Optional[str]:
    """
    Execute an allowlisted plan_runtime tool and return a user-facing reply string.
    Returns None if the tool name is not in the allowlist (orchestrator continues normally).
    """
    tool_name = str(tool_call.get("name") or "")
    tool_args = tool_call.get("arguments") or {}
    if not isinstance(tool_args, dict):
        tool_args = {}

    registry = _build_tool_registry()
    handler = registry.get(tool_name)
    if handler is None:
        logger.warning("[TOOL] Unknown tool_call name=%r for user=%s — skipping", tool_name, user_id)
        return None

    try:
        result = handler(user_id, tool_args)
        log_metric("plan_tool_executed", extra={"user_id": user_id, "tool": tool_name})
    except ValueError as exc:
        logger.warning("[TOOL] tool=%s user=%s failed: %s", tool_name, user_id, exc)
        return f"⚠️ {exc}"
    except Exception as exc:
        logger.error("[TOOL] tool=%s user=%s error: %s", tool_name, user_id, exc, exc_info=True)
        return "⚠️ Не вдалось виконати дію. Спробуй ще раз."

    # Special case: get_plan_status returns a dict to format
    if tool_name == "get_plan_status":
        if result.get("plan_active"):
            return (
                f"📋 Стан: активний план\n"
                f"День {result.get('days_completed', 0)} з {result.get('days_total', 0)}"
            )
        return "📋 Активного плану зараз немає."

    # needs_evening_time soft result from create_followup_plan
    if isinstance(result, dict) and result.get("status") == "needs_evening_time":
        await session_memory.set_pending_action(user_id, "collect_evening_time_for_medium")
        return "О котрій зручно отримувати вечірній момент? Напиши час у форматі 20:30"

    template = _TOOL_REPLY_TEMPLATES.get(tool_name, "✅ Готово.")
    return template


async def handle_incoming_message(
    user_id: int,
    message_text: str,
    defer_plan_draft: bool = False,
) -> Dict[str, Any]:
    """
    Main orchestrator:
    - appends message to session memory
    - auto-completes plan if needed
    - builds user context (FSM state, history, etc.)
    - if state is IDLE_NEW or ONBOARDING:* → calls onboarding handler, returns
    - else → calls coach_agent directly
    - handles generated_plan_object, plan_updates, FSM transition signal
    - returns reply
    """

    await session_memory.append_message(user_id, "user", message_text)

    _auto_complete_plan_if_needed_for_user_id(user_id)

    async def _finalize_reply(
        text: str,
        defer_draft: bool = False,
        plan_draft_parameters: Optional[Dict[str, Any]] = None,
        followup_messages: Optional[List[str]] = None,
        show_plan_actions: bool = False,
        keyboard: Any = None,
    ) -> Dict[str, Any]:
        if not defer_draft:
            await session_memory.append_message(user_id, "assistant", text)
        return {
            "reply_text": text,
            "defer_plan_draft": defer_draft,
            "plan_draft_parameters": plan_draft_parameters,
            "followup_messages": followup_messages or [],
            "show_plan_actions": show_plan_actions,
            "keyboard": keyboard,
        }

    context_payload = await build_user_context(user_id, message_text)
    current_state = context_payload.get("current_state")

    if current_state == SCHEDULE_ADJUSTMENT:
        await session_memory.set_schedule_adjustment_last_active(user_id)
        await session_memory.clear_schedule_adjustment_soft_prompted(user_id)

    # Inject completion_context for IDLE_FINISHED state
    if current_state == "IDLE_FINISHED":
        with SessionLocal() as db:
            completion_context = _build_idle_finished_context(db, user_id)
        if completion_context is not None:
            context_payload["completion_context"] = completion_context

    # Onboarding path — state-based branch, not routing
    if current_state == "IDLE_NEW" or (
        isinstance(current_state, str) and current_state.startswith("ONBOARDING:")
    ):
        onboarding_payload = {
            "user_id": user_id,
            **context_payload,
            "message_text": message_text,
        }
        onboarding_result = await mock_onboarding_agent(onboarding_payload)
        return await _finalize_reply(str(onboarding_result.get("reply_text") or ""))

    # All live-user states → coach_agent directly
    coach_payload = {
        "user_id": user_id,
        **context_payload,
        "message_text": message_text,
    }
    worker_result = await coach_agent(coach_payload)

    reply_text = str(worker_result.get("reply_text") or "")
    defer_draft = False
    plan_draft_parameters: Optional[Dict[str, Any]] = None
    show_plan_actions = False

    error_payload = worker_result.get("error")
    if error_payload is not None:
        if error_payload.get("code") == "CONTRACT_MISMATCH":
            log_metric(
                "plan_contract_mismatch",
                extra={"user_id": user_id, "agent": "coach"},
            )
        log_metric(
            "plan_agent_error",
            extra={
                "timestamp": datetime.utcnow().isoformat(),
                "user_id": user_id,
                "agent": "coach",
                "error": error_payload,
            },
        )
        logger.warning(
            "[PLAN_AGENT] Error payload received for user %s (agent=coach): %s",
            user_id,
            error_payload,
        )
        return await _finalize_reply(reply_text)

    # ── Tool call execution (T5.8B) ───────────────────────────────────────────
    # Coach returns {"tool_call": {"name": "...", "arguments": {...}}}
    # Orchestrator executes allowlisted plan_runtime tools, then returns
    # a deterministic confirmation message — no second LLM round-trip needed.
    # Rule: tool_call is processed before transition_signal / generated_plan_object.
    raw_tool_call = worker_result.get("tool_call")
    if raw_tool_call and isinstance(raw_tool_call, dict):
        tool_result = await _execute_plan_tool(user_id, raw_tool_call)
        if tool_result is not None:
            return await _finalize_reply(tool_result)

    plan_persisted = False
    generated_plan_object = worker_result.get("generated_plan_object")
    if generated_plan_object is not None:
        with SessionLocal() as db:
            user: Optional[User] = db.query(User).filter(User.id == user_id).first()
            if not user:
                logger.warning(
                    "[PLAN] Generated plan ignored — user %s not found (agent=coach)",
                    user_id,
                )
                return await _finalize_reply(reply_text)
            try:
                _persist_generated_plan(db, user, generated_plan_object)
                db.commit()
            except (IntegrityError, PlanAgentEnvelopeError) as exc:
                db.rollback()
                logger.error(
                    "[PLAN] Failed to persist generated plan for user %s (agent=coach)",
                    user_id,
                    exc_info=exc,
                )
                log_metric(
                    "plan_validation_rejected",
                    extra={"user_id": user_id, "agent": "coach"},
                )
                fallback_text = _plan_agent_fallback_envelope().get("reply_text", "")
                return await _finalize_reply(fallback_text)
            else:
                logger.info(
                    "[PLAN] Generated plan persisted for user %s (agent=coach)",
                    user_id,
                )
                plan_persisted = True
                log_metric(
                    "plan_generated_ok",
                    extra={"user_id": user_id, "agent": "coach"},
                )

    plan_updates = worker_result.get("plan_updates")
    transition_signal = worker_result.get("transition_signal")
    if plan_updates and isinstance(plan_updates, dict):
        allowed_execution_adaptations = {"pause", "resume", "PAUSE_PLAN", "RESUME_PLAN"}
        should_persist_updates = bool(generated_plan_object) or (
            plan_updates.get("adaptation_type") in allowed_execution_adaptations
        )
        if not should_persist_updates:
            logger.info(
                "[PLAN] Skipping plan updates outside allowed persistence window (user=%s, agent=coach, state=%s)",
                user_id,
                current_state,
            )
        elif "adaptation_type" in plan_updates:
            if plan_updates.get("adaptation_type") not in allowed_execution_adaptations:
                logger.info(
                    "[PLAN] Skipping non-execution adaptation type %s for user %s (agent=coach)",
                    plan_updates.get("adaptation_type"),
                    user_id,
                )
                return await _finalize_reply(reply_text)
            adaptation_result = None
            with SessionLocal() as db:
                user: Optional[User] = db.query(User).filter(User.id == user_id).first()
                if not user:
                    logger.warning(
                        "[PLAN] Adaptation ignored — user %s not found (agent=coach)",
                        user_id,
                    )
                    return await _finalize_reply(reply_text)
                active_plan = (
                    db.query(AIPlan)
                    .filter(AIPlan.user_id == user_id, AIPlan.status == "active")
                    .order_by(AIPlan.created_at.desc())
                    .first()
                )
                if not active_plan:
                    logger.warning(
                        "[PLAN] Adaptation ignored — active plan missing (user=%s, agent=coach)",
                        user_id,
                    )
                    return await _finalize_reply(reply_text)
                try:
                    adaptation_result = apply_plan_adaptation(db, active_plan.id, plan_updates)
                    db.commit()
                except (PlanAdaptationError, IntegrityError) as exc:
                    db.rollback()
                    logger.error(
                        "[PLAN] Failed to apply adaptation for user %s (agent=coach): %s",
                        user_id,
                        exc,
                    )
                    log_metric(
                        "plan_adaptation_failed",
                        extra={
                            "user_id": user_id,
                            "agent": "coach",
                            "adaptation_type": plan_updates.get("adaptation_type"),
                        },
                    )
                else:
                    log_metric(
                        "plan_adaptation_applied",
                        extra={
                            "user_id": user_id,
                            "agent": "coach",
                            "adaptation_type": adaptation_result.adaptation_type,
                            "scope": adaptation_result.scope,
                            "step_diff_count": adaptation_result.step_diff_count,
                        },
                    )
            if adaptation_result:
                if adaptation_result.canceled_step_ids:
                    cancel_plan_step_jobs(adaptation_result.canceled_step_ids)
                if adaptation_result.rescheduled_step_ids:
                    reschedule_plan_steps(adaptation_result.rescheduled_step_ids)
        else:
            with SessionLocal() as db:
                user: Optional[User] = db.query(User).filter(User.id == user_id).first()
                if not user:
                    logger.warning(
                        "[PLAN] Updates ignored — user %s not found (agent=coach)",
                        user_id,
                    )
                    return await _finalize_reply(reply_text)
                try:
                    if "plan_end_date" in plan_updates:
                        raw_end_date = plan_updates.get("plan_end_date")
                        if raw_end_date:
                            user.plan_end_date = datetime.fromisoformat(str(raw_end_date))
                        else:
                            user.plan_end_date = None
                    db.commit()
                except (ValueError, IntegrityError):
                    db.rollback()
                    logger.error(
                        "[PLAN] Failed to persist updates for user %s (agent=coach)",
                        user_id,
                    )
                else:
                    logger.info(
                        "[PLAN] User %s updated: end=%s",
                        user_id,
                        user.plan_end_date,
                    )

    # FSM guard enforced via _guard_fsm_transition/can_transition.
    next_state, rejection_reason = _guard_fsm_transition(
        context_payload.get("current_state"),
        transition_signal,
        "coach",
        plan_persisted=plan_persisted,
    )
    if transition_signal is not None and next_state is None:
        logger.warning(
            "[FSM] Ignoring transition_signal for user %s: %s (reason=%s, agent=coach)",
            user_id,
            transition_signal,
            rejection_reason or "invalid_state",
        )
        log_metric(
            "fsm_transition_blocked",
            extra={
                "user_id": user_id,
                "agent": "coach",
                "current_state": context_payload.get("current_state"),
                "transition_signal": transition_signal,
                "reason": rejection_reason or "invalid_state",
            },
        )
        defer_draft = False
        plan_draft_parameters = None
    elif next_state is not None:
        previous_state = await _commit_fsm_transition(user_id, "coach", next_state)
        if previous_state is None:
            return await _finalize_reply(reply_text)

    return await _finalize_reply(
        reply_text,
        defer_draft=defer_draft,
        plan_draft_parameters=plan_draft_parameters,
        show_plan_actions=show_plan_actions,
    )
