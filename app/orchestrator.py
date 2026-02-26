import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import logging

import pytz
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ai_plans import PlanAgentEnvelopeError, plan_agent
from app.ai_router import cognitive_route_message
from app.db import (
    AIPlan,
    AIPlanDay,
    AIPlanStep,
    ChatHistory,
    ContentLibrary,
    AdaptationHistory,
    PlanInstance,
    SessionLocal,
    User,
    UserProfile,
)
from app.logging.router_logging import log_metric, log_router_decision
from app.plan_adaptations import PlanAdaptationError, apply_plan_adaptation
from app.adaptation_executor import AdaptationExecutor, AdaptationResult
from app.adaptation_types import (
    AdaptationIntent,
    AdaptationNotEligibleError,
    check_adaptation_conflict,
    check_rate_limit,
    get_inverse_intent,
)
from app.plan_parameters import normalize_plan_parameters
from app.scheduler import cancel_plan_step_jobs, reschedule_plan_steps
from app.redis_client import redis_client
from app.session_memory import SessionMemory
from app.time_slots import compute_scheduled_for, resolve_daily_time_slots
from app.ux.plan_messages import build_activation_info_message
from app.ux.adaptation_preview import build_adaptation_preview, build_adaptation_success_message
from app.plan_drafts.service import (
    DraftValidationError,
    InsufficientLibraryError,
    build_plan_draft,
    delete_latest_draft,
    get_latest_draft,
    overwrite_plan_draft,
    persist_plan_draft,
)
from app.plan_drafts.preview import build_confirmation_preview, render_confirmation_preview
from app.plan_finalization import (
    ActivePlanExistsError,
    DraftNotFoundError,
    FinalizationError,
    InvalidDraftError,
    activate_plan_side_effects,
    finalize_plan,
    validate_for_finalization,
)
from app.workers.coach_agent import coach_agent
from app.fsm.guards import can_transition
from app.fsm.states import (
    ADAPTATION_CONFIRMATION,
    ADAPTATION_FLOW_ALLOWED_TRANSITIONS,
    ADAPTATION_FLOW_ENTRY_STATES,
    ADAPTATION_FLOW_STATES,
    ADAPTATION_PARAMS,
    ADAPTATION_SELECTION,
    FSM_ALLOWED_STATES,
    PLAN_FLOW_ALLOWED_TRANSITIONS,
    PLAN_FLOW_ENTRYPOINTS,
    PLAN_FLOW_STATES,
    ADAPTATION_ENTRY_STATES,
    ENTRY_PROMPT_ALLOWED_STATES,
    PLAN_CREATION_ENTRY_STATES,
)
from app.workers.mock_workers import (
    mock_manager_agent,
    mock_onboarding_agent,
    mock_safety_agent,
)
from app.schemas.planner import DifficultyLevel, GeneratedPlan, StepType
from app.plan_duration import assert_canonical_total_days
from app.telemetry import log_user_event

session_memory = SessionMemory(limit=20)
logger = logging.getLogger(__name__)

PLAN_CONTRACT_VERSION = "v1"
PLAN_SCHEMA_VERSION = "v1"
PLAN_GENERATION_WAIT_MESSAGE = "⏳ План генерується…"
PLAN_GENERATION_ERROR_MESSAGE = (
    "⚠️ Не вдалося згенерувати план.\nСпробуй ще раз або зміни параметри."
)
PLAN_FINALIZATION_ERROR_MESSAGE = "⚠️ Не вдалося активувати план."
PLAN_DURATION_VALUES = {"SHORT", "MEDIUM", "STANDARD", "LONG"}
PLAN_FOCUS_VALUES = {"SOMATIC", "COGNITIVE", "BOUNDARIES", "REST", "MIXED"}
PLAN_LOAD_VALUES = {"LITE", "MID", "INTENSIVE"}
PLAN_TIME_SLOT_VALUES = {"MORNING", "DAY", "EVENING"}
INTENSIVE_AUTO_SLOTS = ["MORNING", "DAY", "EVENING"]


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


def _sanitize_plan_updates(plan_updates: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(plan_updates, dict):
        return None
    clean_updates: Dict[str, Any] = {}
    for key, value in plan_updates.items():
        if value is None:
            continue
        if key == "duration" and value in PLAN_DURATION_VALUES:
            clean_updates[key] = value
        elif key == "focus" and value in PLAN_FOCUS_VALUES:
            clean_updates[key] = value
        elif key == "load" and value in PLAN_LOAD_VALUES:
            clean_updates[key] = value
        elif key == "preferred_time_slots":
            if not isinstance(value, list) or not value:
                continue
            slots = [slot for slot in value if slot in PLAN_TIME_SLOT_VALUES]
            if slots:
                clean_updates[key] = slots
    return clean_updates


def run_plan_tool_call(tool_call: Dict[str, Any]) -> Dict[str, Any]:
    """Backward-compatible handler for legacy plan tool call payloads."""
    tool_name = str(tool_call.get("name") or "")
    if tool_name == "start_plan":
        return {"user_text": "Starting a plan. Tell me what you'd like to plan."}
    return {"user_text": ""}


async def handle_confirmation_pending_action(
    user_id: int,
    plan_updates: Any,
    transition_signal: Any,
    reply_text: str,
    context_payload: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    # CONFIRMATION_PENDING is intentionally minimal.
    # There are only two actions:
    # - FSM transition
    # - rebuild (plan_updates != {})
    # Any other logic is a bug.
    current_state = context_payload.get("current_state")
    if current_state != "PLAN_FLOW:CONFIRMATION_PENDING":
        return None

    if transition_signal == "PLAN_FLOW:DATA_COLLECTION" and plan_updates is None:
        await session_memory.clear_plan_parameters(user_id)
        with SessionLocal() as db:
            delete_latest_draft(db, user_id)
            db.commit()
        log_metric("plan_flow_restarted", extra={"user_id": user_id})
        log_metric("plan_draft_deleted", extra={"user_id": user_id})
        return None

    if transition_signal == "IDLE_PLAN_ABORTED":
        await session_memory.clear_plan_parameters(user_id)
        with SessionLocal() as db:
            delete_latest_draft(db, user_id)
            db.commit()
        log_metric("plan_flow_aborted", extra={"user_id": user_id})
        log_metric("plan_draft_deleted", extra={"user_id": user_id})
        return None

    if transition_signal is None and isinstance(plan_updates, dict):
        parameters_for_draft = context_payload.get("known_parameters") or {}
        action = None
        if plan_updates:
            action = "plan_draft_rebuilt_parameters"
        if action:
            try:
                draft = build_plan_draft(parameters_for_draft, user_id=str(user_id))
                with SessionLocal() as db:
                    overwrite_plan_draft(db, user_id, draft)
                    db.commit()
                log_metric(action, extra={"user_id": user_id})
                preview = build_confirmation_preview(draft, parameters_for_draft)
                rendered_preview = render_confirmation_preview(preview)
                return {"reply_text": rendered_preview, "show_plan_actions": True}
            except DraftValidationError as exc:
                logger.error(
                    "[PLAN_DRAFT] Draft update validation failed for user %s: %s (duration=%s focus=%s load=%s slots=%s)",
                    user_id,
                    exc,
                    parameters_for_draft.get("duration"),
                    parameters_for_draft.get("focus"),
                    parameters_for_draft.get("load"),
                    parameters_for_draft.get("preferred_time_slots"),
                )
                return {"reply_text": PLAN_GENERATION_ERROR_MESSAGE, "show_plan_actions": False}
            except (InsufficientLibraryError, IntegrityError) as exc:
                logger.error(
                    "[PLAN_DRAFT] Draft update failed for user %s: %s",
                    user_id,
                    exc,
                )
                return {"reply_text": PLAN_GENERATION_ERROR_MESSAGE, "show_plan_actions": False}
    return None


def _normalize_confirmation_reply(payload: Any) -> Optional[Dict[str, Any]]:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        return None
    reply_text = payload.get("reply_text")
    if not isinstance(reply_text, str):
        return None
    show_plan_actions = payload.get("show_plan_actions", False)
    if not isinstance(show_plan_actions, bool):
        return None
    return {"reply_text": reply_text, "show_plan_actions": show_plan_actions}


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
    target_agent: str,
    plan_persisted: bool = False,
) -> Tuple[Optional[str], Optional[str]]:
    if transition_signal is None:
        return None, None

    normalized_current = _normalize_fsm_state(current_state) if current_state else None
    if normalized_current in PLAN_FLOW_STATES or normalized_current in ADAPTATION_FLOW_STATES:
        if target_agent == "coach":
            return None, None
        if target_agent != "plan":
            return None, "non_plan_agent_in_tunnel"

    normalized_signal = _normalize_fsm_state(transition_signal)
    if normalized_signal is None:
        if transition_signal in {"EXECUTE_ADAPTATION", "UNDO_LAST_ADAPTATION"}:
            return transition_signal, None
        return None, "invalid_state"

    if normalized_current is None:
        return normalized_signal, None

    # PLAN finalization has additional runtime requirement beyond state graph validity.
    if normalized_current == "PLAN_FLOW:FINALIZATION" and normalized_signal == "ACTIVE":
        if not plan_persisted:
            return None, "plan_flow_exit_blocked_not_persisted"

    if not can_transition(normalized_current, normalized_signal):
        return None, "transition_blocked_by_guards"

    return normalized_signal, None


def _plan_end_date_status(plan_end_date: Optional[datetime]) -> Optional[Tuple[datetime, datetime]]:
    if not plan_end_date:
        return None
    if plan_end_date.tzinfo is None:
        return plan_end_date, datetime.utcnow()
    return plan_end_date.astimezone(pytz.UTC), datetime.now(pytz.UTC)


def _auto_complete_plan_if_needed(user_id: int) -> None:
    with SessionLocal() as db:
        user: Optional[User] = db.query(User).filter(User.id == user_id).first()
        if not user:
            return
        if user.current_state != "ACTIVE":
            return
        plan_times = _plan_end_date_status(user.plan_end_date)
        if not plan_times:
            return
        plan_end_date, now = plan_times
        if plan_end_date >= now:
            return
        user.current_state = "IDLE_FINISHED"
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            logger.error(
                "[FSM] Failed to auto-complete plan for user %s (plan_end_date=%s)",
                user_id,
                plan_end_date,
            )
        else:
            logger.info(
                "[FSM] Auto-completed plan for user %s: ACTIVE → IDLE_FINISHED (plan_end_date=%s)",
                user_id,
                plan_end_date,
            )


def _auto_drop_plan_for_new_flow(user_id: int) -> bool:
    with SessionLocal() as db:
        user: Optional[User] = db.query(User).filter(User.id == user_id).first()
        if not user:
            return False
        if user.current_state not in {"ACTIVE", "ACTIVE_PAUSED", "ACTIVE_PAUSED_CONFIRMATION"}:
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
        "[FSM] Auto-dropped plan before new PLAN_FLOW for user %s",
        user_id,
    )
    return True


async def _commit_fsm_transition(
    user_id: int,
    target_agent: str,
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
                target_agent,
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
            target_agent,
            reason,
        )
        log_router_decision(
            {
                "event_type": "fsm_transition",
                "user_id": user_id,
                "agent": target_agent,
                "from_state": previous_state,
                "to_state": next_state,
                "reason": reason,
            }
        )
        return previous_state

    if db is not None:
        previous_state = _apply_transition(db)
        if next_state == "PLAN_FLOW:DATA_COLLECTION" and previous_state not in PLAN_FLOW_STATES:
            await session_memory.clear_plan_parameters(user_id)
        return previous_state

    with SessionLocal() as managed_db:
        previous_state = _apply_transition(managed_db)
        managed_db.commit()

    if next_state == "PLAN_FLOW:DATA_COLLECTION" and previous_state not in PLAN_FLOW_STATES:
        await session_memory.clear_plan_parameters(user_id)
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
    # Design rule: every adaptation creates a new plan version and abandons the previous one.
    adaptation_version = (latest_plan.adaptation_version + 1) if latest_plan else 1
    if latest_plan and latest_plan.status == "active":
        latest_plan.status = "abandoned"

    known_parameters = normalize_plan_parameters(plan_payload.get("known_parameters"))
    plan_load = (known_parameters.get("load") or plan_payload.get("load"))
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
        adaptation_version=adaptation_version,
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

    log_router_decision(
        {
            "event_type": "plan_snapshot",
            "timestamp": datetime.utcnow().isoformat(),
            "user_id": user.id,
            "plan_summary": parsed_plan.title,
            "plan_key_parameters": {
                "module_id": str(parsed_plan.module_id),
                "duration_days": parsed_plan.duration_days,
                "schedule_days": len(parsed_plan.schedule),
                "milestones": len(parsed_plan.milestones),
            },
        }
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
    known_parameters = normalize_plan_parameters(
        await session_memory.get_plan_parameters(user_id)
    )

    return {
        "message_text": message_text,
        "short_term_history": stm_history,
        "profile_snapshot": ltm_snapshot,
        "current_state": fsm_state,
        "temporal_context": temporal_context,
        "known_parameters": known_parameters,
    }


async def call_router(user_id: int, message_text: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Сервісний хелпер: збирає контекст, формує payload для Router'а,
    викликає router і повертає JSON-відповідь (target_agent, confidence, intent_bucket).
    """

    context_payload = context or await build_user_context(user_id, message_text)

    # STRICT: Router only reads user_id, current_state, latest_user_message, short_term_history
    router_input = {
        "user_id": user_id,
        "current_state": context_payload.get("current_state"),
        "latest_user_message": context_payload.get("message_text", message_text),
        "short_term_history": context_payload.get("short_term_history"),
    }

    router_output = await cognitive_route_message(router_input)
    return {
        "router_result": router_output.get("router_result", {}),
        "router_meta": router_output.get("router_meta", {}),
        "fsm_state": context_payload.get("current_state"),
        "session_id": None,
        "input_message": message_text,
        "context_payload": context_payload,
    }




def get_active_plan(db: Session, user_id: int) -> Optional[AIPlan]:
    return (
        db.query(AIPlan)
        .filter(AIPlan.user_id == user_id, AIPlan.status.in_(["active", "paused"]))
        .order_by(AIPlan.created_at.desc())
        .first()
    )


def compute_available_adaptations(db: Session, plan: AIPlan) -> List[AdaptationIntent]:
    available: List[AdaptationIntent] = []

    current_daily_count = get_daily_task_count(db, plan)
    if current_daily_count > 1:
        available.append(AdaptationIntent.REDUCE_DAILY_LOAD)
    if current_daily_count < 3:
        available.append(AdaptationIntent.INCREASE_DAILY_LOAD)

    steps = (
        db.query(AIPlanStep)
        .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
        .filter(AIPlanDay.plan_id == plan.id)
        .all()
    )
    if steps:
        difficulty_map = {
            "EASY": 1,
            "MEDIUM": 2,
            "HARD": 3,
        }

        normalized_difficulties = [
            difficulty_map.get(str(step.difficulty).upper(), 1)
            for step in steps
        ]
        min_difficulty = min(normalized_difficulties)
        max_difficulty = max(normalized_difficulties)

        if max_difficulty > 1:
            available.append(AdaptationIntent.LOWER_DIFFICULTY)
        if min_difficulty < 3:
            available.append(AdaptationIntent.INCREASE_DIFFICULTY)

    current_day = plan.current_day or 1

    # EXTEND: дозволено якщо є куди розширювати
    if plan.total_days in {7, 14, 21}:
        available.append(AdaptationIntent.EXTEND_PLAN_DURATION)

    # SHORTEN: показуємо якщо current_day < total_days.
    # Детальна перевірка current_day > target_days — в executor.
    if plan.total_days in {21, 90} and current_day < plan.total_days:
        available.append(AdaptationIntent.SHORTEN_PLAN_DURATION)

    if plan.status == "active":
        available.append(AdaptationIntent.PAUSE_PLAN)
    elif plan.status == "paused":
        available.append(AdaptationIntent.RESUME_PLAN)

    available.append(AdaptationIntent.CHANGE_MAIN_CATEGORY)
    return available


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
            AIPlanStep.canceled_by_adaptation == False,
            AIPlanStep.skipped == False,
            AIPlanStep.is_completed == False,
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


async def build_adaptation_payload(
    user_id: int,
    message_text: str,
    current_state: str,
    db: Session,
) -> Dict[str, Any]:
    active_plan = get_active_plan(db, user_id)
    if not active_plan:
        raise ValueError("No active plan for adaptation")

    adaptation_context = await session_memory.get_adaptation_context(user_id) or {
        "intent": None,
        "params": None,
    }

    payload: Dict[str, Any] = {
        "current_state": current_state,
        "message_text": message_text,
        "adaptation_context": adaptation_context,
    }

    if current_state == ADAPTATION_SELECTION:
        available = compute_available_adaptations(db, active_plan)
        payload["available_adaptations"] = [intent.value for intent in available]
        payload["active_plan"] = {
            "load": active_plan.load,
            "duration": active_plan.total_days,
            "status": active_plan.status,
            "preferred_time_slots": active_plan.preferred_time_slots or [],
        }
    elif current_state == ADAPTATION_PARAMS:
        payload["active_plan"] = {
            "duration": active_plan.total_days,
            "load": active_plan.load,
            "preferred_time_slots": active_plan.preferred_time_slots or [],
            "current_day": active_plan.current_day or 1,
            "focus": (active_plan.focus or "").lower(),
        }
    elif current_state == ADAPTATION_CONFIRMATION:
        payload["active_plan"] = {
            "load": active_plan.load,
            "duration": active_plan.total_days,
            "focus": active_plan.focus,
            "daily_task_count": get_daily_task_count(db, active_plan),
            "difficulty_level": get_avg_difficulty(db, active_plan),
            "status": active_plan.status,
            "current_day": active_plan.current_day or 1,
        }

    return payload


def check_adaptation_eligibility(
    db: Session,
    plan: AIPlan,
    intent: AdaptationIntent,
) -> str | None:
    """Full pre-execution eligibility check."""
    last_entry = (
        db.query(AdaptationHistory)
        .filter(
            AdaptationHistory.plan_id == plan.id,
            AdaptationHistory.is_rolled_back == False,
        )
        .order_by(AdaptationHistory.applied_at.desc())
        .first()
    )
    last_intent = None
    if last_entry:
        try:
            last_intent = AdaptationIntent(last_entry.intent)
        except ValueError:
            pass

    conflict_reason = check_adaptation_conflict(
        intent=intent,
        last_applied_intent=last_intent,
        current_plan_load=plan.load,
        current_plan_status=plan.status,
    )
    if conflict_reason:
        return conflict_reason

    all_history = (
        db.query(AdaptationHistory)
        .filter(AdaptationHistory.plan_id == plan.id)
        .order_by(AdaptationHistory.applied_at.desc())
        .all()
    )
    now_utc = datetime.now(timezone.utc)
    return check_rate_limit(intent, all_history, now_utc)


async def handle_undo_last_adaptation(
    user_id: int,
    db: Session,
    current_state: str,
) -> tuple[str, list[str]]:
    """Handles semantic undo: cancel last adaptation via inverse intent."""
    active_plan = get_active_plan(db, user_id)
    if not active_plan:
        # Known limitation: `adaptation_undo_blocked` analytics are plan-scoped.
        # Logging this event with `plan_id=None` causes a silent mismatch in
        # acceptance-rate queries that filter by plan_id.
        await session_memory.clear_adaptation_context(user_id)
        return "Немає активного плану.", []

    try:
        log_user_event(
            db=db,
            user_id=user_id,
            event_type="adaptation_undo_requested",
            context={
                "plan_id": str(active_plan.id) if active_plan else None,
                "current_state": current_state,
            },
        )
    except Exception:
        logger.warning("[ADAPTATION] Failed to log undo_requested for user %s", user_id, exc_info=True)

    last_entry = (
        db.query(AdaptationHistory)
        .filter(
            AdaptationHistory.plan_id == active_plan.id,
            AdaptationHistory.is_rolled_back == False,
        )
        .order_by(AdaptationHistory.applied_at.desc())
        .first()
    )
    if not last_entry:
        try:
            log_user_event(
                db=db,
                user_id=user_id,
                event_type="adaptation_undo_blocked",
                context={
                    "plan_id": str(active_plan.id),
                    "reason": "no_history",
                    "last_intent": None,
                },
            )
        except Exception:
            logger.warning("[ADAPTATION] Failed to log undo_blocked for user %s", user_id, exc_info=True)
        await session_memory.clear_adaptation_context(user_id)
        return "Немає змін для скасування.", []

    try:
        last_intent = AdaptationIntent(last_entry.intent)
    except ValueError:
        try:
            log_user_event(
                db=db,
                user_id=user_id,
                event_type="adaptation_undo_blocked",
                context={
                    "plan_id": str(active_plan.id),
                    "reason": "invalid_intent_in_history",
                    "last_intent": last_entry.intent if last_entry else None,
                },
            )
        except Exception:
            logger.warning("[ADAPTATION] Failed to log undo_blocked for user %s", user_id, exc_info=True)
        await session_memory.clear_adaptation_context(user_id)
        return "Не вдалось визначити останню адаптацію.", []

    inverse = get_inverse_intent(last_intent)
    if inverse is None:
        try:
            log_user_event(
                db=db,
                user_id=user_id,
                event_type="adaptation_undo_blocked",
                context={
                    "plan_id": str(active_plan.id),
                    "reason": "non_invertible_intent",
                    "last_intent": last_intent.value if last_intent else None,
                },
            )
        except Exception:
            logger.warning("[ADAPTATION] Failed to log undo_blocked for user %s", user_id, exc_info=True)
        await session_memory.clear_adaptation_context(user_id)
        return (
            f"❌ Цю зміну ({last_intent.value}) неможливо скасувати автоматично.\n"
            "Структурні зміни плану потребують ручного коригування.",
            [],
        )

    original_params = last_entry.params or {}
    inverse_params: dict | None = None

    if last_intent == AdaptationIntent.REDUCE_DAILY_LOAD:
        slot = original_params.get("slot_to_remove")
        if slot:
            inverse_params = {"slot_to_add": slot}

    elif last_intent == AdaptationIntent.INCREASE_DAILY_LOAD:
        slot = original_params.get("slot_to_add")
        if slot:
            inverse_params = {"slot_to_remove": slot}
        else:
            try:
                log_user_event(
                    db=db,
                    user_id=user_id,
                    event_type="adaptation_undo_blocked",
                    context={
                        "plan_id": str(active_plan.id),
                        "reason": "missing_slot_params",
                        "last_intent": last_intent.value if last_intent else None,
                    },
                )
            except Exception:
                logger.warning("[ADAPTATION] Failed to log undo_blocked for user %s", user_id, exc_info=True)
            await session_memory.clear_adaptation_context(user_id)
            return (
                "❌ Не вдалось скасувати цю зміну автоматично.\n"
                "Параметр слоту не збережено. Зміни навантаження вручну.",
                [],
            )

    fake_llm_response = {
        "adaptation_intent": inverse.value,
        "adaptation_params": inverse_params,
        "transition_signal": "EXECUTE_ADAPTATION",
        "reply_text": "",
    }
    return await handle_adaptation_response(
        user_id=user_id,
        llm_response=fake_llm_response,
        current_state=current_state,
        db=db,
        skip_eligibility_checks=True,
    )


async def execute_adaptation(
    user_id: int,
    llm_response: Dict[str, Any],
    db: Session,
) -> AdaptationResult:
    """Returns post-commit scheduler actions for adaptation."""
    adaptation_intent_str = llm_response.get("adaptation_intent")
    adaptation_params = llm_response.get("adaptation_params")

    try:
        intent = AdaptationIntent(adaptation_intent_str)
    except ValueError:
        logger.error("Invalid adaptation_intent: %s", adaptation_intent_str)
        raise

    active_plan = get_active_plan(db, user_id)
    if not active_plan:
        raise ValueError("No active plan for adaptation")

    executor = AdaptationExecutor()
    return executor.execute(
        db=db,
        plan_id=active_plan.id,
        intent=intent,
        params=adaptation_params,
    )


async def handle_adaptation_response(
    user_id: int,
    llm_response: Dict[str, Any],
    current_state: str,
    db: Session,
    skip_eligibility_checks: bool = False,
) -> Tuple[str, List[str]]:
    """Handle LLM response for adaptation flow using caller session."""
    reply_text = str(llm_response.get("reply_text") or "")
    transition_signal = llm_response.get("transition_signal")
    adaptation_intent = llm_response.get("adaptation_intent")
    adaptation_params = llm_response.get("adaptation_params")
    followups: List[str] = []

    if adaptation_intent or adaptation_params:
        await session_memory.update_adaptation_context(
            user_id,
            {"intent": adaptation_intent, "params": adaptation_params},
        )

    if transition_signal == "UNDO_LAST_ADAPTATION":
        return await handle_undo_last_adaptation(user_id, db, current_state)

    if transition_signal == "EXECUTE_ADAPTATION":
        adaptation_intent_str = llm_response.get("adaptation_intent")
        next_state_after = "ACTIVE"
        _intent: AdaptationIntent | None = None
        try:
            _intent = AdaptationIntent(adaptation_intent_str)
            if _intent == AdaptationIntent.PAUSE_PLAN:
                next_state_after = "ACTIVE_PAUSED"
        except (ValueError, TypeError):
            pass

        step_ids_to_reschedule: list[int] = []
        step_ids_to_cancel: list[int] = []
        adaptation_applied = False
        try:
            active_plan = get_active_plan(db, user_id)
            if not active_plan:
                followups.append("Немає активного плану для адаптації.")
                next_state_after = current_state
            else:
                if _intent is None:
                    followups.append("Невалідний тип адаптації.")
                    return reply_text or "Ця зміна зараз недоступна.", followups
                if not skip_eligibility_checks:
                    eligibility_reason = check_adaptation_eligibility(db, active_plan, _intent)
                    if eligibility_reason:
                        followups.append(f"Адаптація недоступна: {eligibility_reason}")
                        return reply_text or "Ця зміна зараз недоступна.", followups
                adaptation_result = await execute_adaptation(user_id, llm_response, db)
                step_ids_to_reschedule = adaptation_result.step_ids_to_reschedule
                step_ids_to_cancel = adaptation_result.step_ids_to_cancel
                adaptation_applied = True
        except AdaptationNotEligibleError as exc:
            logger.warning(
                "[ADAPTATION] Not eligible for user %s: %s", user_id, exc.reason
            )
            followups.append(f"Ця дія недоступна: {exc.reason}")
            next_state_after = current_state
        except NotImplementedError:
            db.rollback()
            followups.append("Ця адаптація ще в розробці. Спробуй іншу зміну.")
            next_state_after = current_state
        except Exception:
            db.rollback()
            logger.exception("Adaptation execution failed for user %s", user_id)
            followups.append("Не вдалось застосувати зміни. Спробуй пізніше.")
            next_state_after = current_state

        if adaptation_applied:
            # next_state_after is final here (updated in exception branches when needed).
            # FSM transition — commits user.current_state + plan.status зміни разом
            if not can_transition(current_state, next_state_after):
                logger.error(
                    "[FSM] Blocked illegal transition %s -> %s",
                    current_state,
                    next_state_after,
                )
                db.rollback()  # 🔒 Rollback mutated plan state
                await session_memory.clear_adaptation_context(user_id)
                return reply_text or "Помилка переходу. Спробуй ще раз.", followups
            await _commit_fsm_transition(
                user_id=user_id,
                target_agent="plan",
                next_state=next_state_after,
                db=db,
                reason="adaptation_executed",
            )

            # 🔒 Explicit commit before any scheduler interaction
            db.commit()

            # 🕒 Post-commit side effects only
            if step_ids_to_cancel:
                try:
                    cancel_plan_step_jobs(step_ids_to_cancel)
                except Exception:
                    logger.error(
                        "[ADAPTATION] cancel jobs failed after commit for user %s, step_ids=%s",
                        user_id,
                        step_ids_to_cancel,
                        exc_info=True,
                    )

            if step_ids_to_reschedule:
                try:
                    reschedule_plan_steps(step_ids_to_reschedule)
                except Exception:
                    logger.error(
                        "[ADAPTATION] reschedule failed after commit for user %s, step_ids=%s",
                        user_id,
                        step_ids_to_reschedule,
                        exc_info=True,
                    )

            adaptation_intent_str = llm_response.get("adaptation_intent") or ""
            reply_text = build_adaptation_success_message(adaptation_intent_str)

        await session_memory.clear_adaptation_context(user_id)
        return reply_text, followups

    if transition_signal == "ACTIVE":
        if current_state == "ADAPTATION_CONFIRMATION":
            adaptation_ctx = await session_memory.get_adaptation_context(user_id)
            rejected_intent = (adaptation_ctx or {}).get("intent")
            rejected_params = (adaptation_ctx or {}).get("params")
            if rejected_intent:
                try:
                    log_user_event(
                        db=db,
                        user_id=user_id,
                        event_type="adaptation_rejected",
                        context={
                            "rejected_intent": rejected_intent,
                            "rejected_params": rejected_params,
                            "reason": "user_declined",
                        },
                    )
                except Exception:
                    logger.warning(
                        "[ADAPTATION] Failed to log adaptation_rejected for user %s",
                        user_id,
                        exc_info=True,
                    )

        if not can_transition(current_state, "ACTIVE"):
            logger.error(
                "[FSM] Blocked illegal transition %s -> %s",
                current_state,
                "ACTIVE",
            )
            db.rollback()
            await session_memory.clear_adaptation_context(user_id)
            return reply_text or "Помилка переходу. Спробуй ще раз.", followups
        await _commit_fsm_transition(
            user_id=user_id,
            target_agent="plan",
            next_state="ACTIVE",
            db=db,
            reason="adaptation_aborted",
        )
        await session_memory.clear_adaptation_context(user_id)
        return reply_text, followups

    if transition_signal in ADAPTATION_FLOW_STATES:
        if not can_transition(current_state, transition_signal):
            logger.error(
                "[FSM] Blocked illegal transition %s -> %s",
                current_state,
                transition_signal,
            )
            db.rollback()
            return reply_text or "Помилка переходу. Спробуй ще раз.", followups
        await _commit_fsm_transition(
            user_id=user_id,
            target_agent="plan",
            next_state=transition_signal,
            db=db,
            reason=f"adaptation_flow_{current_state}_to_{transition_signal}",
        )
        return reply_text, followups

    if transition_signal is None:
        return reply_text, followups

    logger.error("Unknown adaptation transition_signal: %s", transition_signal)
    if not can_transition(current_state, "ACTIVE"):
        logger.error(
            "[FSM] Blocked illegal transition %s -> %s",
            current_state,
            "ACTIVE",
        )
        db.rollback()
        await session_memory.clear_adaptation_context(user_id)
        return reply_text or "Помилка переходу. Спробуй ще раз.", followups
    await _commit_fsm_transition(
        user_id=user_id,
        target_agent="plan",
        next_state="ACTIVE",
        db=db,
        reason="invalid_signal_fallback",
    )
    await session_memory.clear_adaptation_context(user_id)
    return reply_text or "Помилка. Спробуй ще раз.", followups


async def handle_adaptation_flow(
    user_id: int,
    message_text: str,
    current_state: str,
    db: Session,
) -> Tuple[str, List[str]]:
    payload = await build_adaptation_payload(user_id, message_text, current_state, db)
    llm_response = await plan_agent(payload)
    reply_text, followups = await handle_adaptation_response(
        user_id, llm_response, current_state, db
    )

    transition = llm_response.get("transition_signal")
    # Inject preview ONLY when staying in ADAPTATION_CONFIRMATION.
    # Exclude all transitions: executing, aborting, going back to params or selection.
    _no_preview_transitions = {
        "EXECUTE_ADAPTATION",
        "ACTIVE",
        "IDLE_PLAN_ABORTED",
        "ADAPTATION_SELECTION",
        "ADAPTATION_PARAMS",  # user asked to edit params — don't show stale confirm card
    }
    if current_state == ADAPTATION_CONFIRMATION and transition not in _no_preview_transitions:
        adaptation_context = await session_memory.get_adaptation_context(user_id) or {}
        intent = adaptation_context.get("intent")
        params = adaptation_context.get("params") or {}
        active_plan_data = payload.get("active_plan") or {}
        if intent:
            preview_text = build_adaptation_preview(intent, params, active_plan_data)
            # Replace reply_text entirely: preview card already ends with "Підтвердити?"
            # so LLM's conversational line is redundant and causes visual duplication.
            reply_text = preview_text

    return reply_text, followups


async def build_plan_draft_preview(
    user_id: int,
    parameters_for_draft: Dict[str, Any],
) -> str:
    try:
        draft = build_plan_draft(parameters_for_draft, user_id=str(user_id))
        with SessionLocal() as db:
            persist_plan_draft(db, user_id, draft)
            db.commit()
    except DraftValidationError as exc:
        logger.error(
            "[PLAN_DRAFT] Draft creation validation failed for user %s: %s (duration=%s focus=%s load=%s slots=%s)",
            user_id,
            exc,
            parameters_for_draft.get("duration"),
            parameters_for_draft.get("focus"),
            parameters_for_draft.get("load"),
            parameters_for_draft.get("preferred_time_slots"),
        )
        return PLAN_GENERATION_ERROR_MESSAGE
    except (InsufficientLibraryError, IntegrityError) as exc:
        logger.error(
            "[PLAN_DRAFT] Draft creation failed for user %s: %s",
            user_id,
            exc,
        )
        return PLAN_GENERATION_ERROR_MESSAGE
    log_metric("plan_draft_created", extra={"user_id": user_id})
    preview = build_confirmation_preview(draft, parameters_for_draft)
    return render_confirmation_preview(preview)


async def handle_incoming_message(
    user_id: int,
    message_text: str,
    defer_plan_draft: bool = False,
) -> Dict[str, Any]:
    """
    Головний оркестратор:
    - збирає контекст
    - викликає Router
    - за target_agent викликає відповідний mock-агент
    - повертає відповідь та метадані для транспорту
    """

    await session_memory.append_message(user_id, "user", message_text)

    _auto_complete_plan_if_needed(user_id)

    async def _finalize_reply(
        text: str,
        defer_draft: bool = False,
        plan_draft_parameters: Optional[Dict[str, Any]] = None,
        followup_messages: Optional[List[str]] = None,
        show_plan_actions: bool = False,
    ) -> Dict[str, Any]:
        if not defer_draft:
            await session_memory.append_message(user_id, "assistant", text)
        return {
            "reply_text": text,
            "defer_plan_draft": defer_draft,
            "plan_draft_parameters": plan_draft_parameters,
            "followup_messages": followup_messages or [],
            "show_plan_actions": show_plan_actions,
        }

    context_payload = await build_user_context(user_id, message_text)

    show_plan_actions = False
    if context_payload.get("current_state") == "PLAN_FLOW:CONFIRMATION_PENDING":
        with SessionLocal() as db:
            latest_draft = get_latest_draft(db, user_id)
            context_payload["draft_plan_artifact"] = (
                latest_draft.draft_data if latest_draft else None
            )
    router_output = await call_router(user_id, message_text, context_payload)

    router_result = router_output.get("router_result", {})
    router_meta = router_output.get("router_meta", {})

    log_payload = {
        "event_type": "router_decision",
        "timestamp": datetime.utcnow().isoformat(),
        "user_id": user_id,
        "session_id": router_output.get("session_id"),
        "input_message": router_output.get("input_message", message_text),
        "fsm_state": router_output.get("fsm_state"),
        "target_agent": router_result.get("target_agent"),
        "confidence": router_result.get("confidence"),
        "intent_bucket": router_result.get("intent_bucket"),
        "llm_prompt_tokens": router_meta.get("llm_prompt_tokens"),
        "llm_response_tokens": router_meta.get("llm_response_tokens"),
        "router_latency_ms": router_meta.get("router_latency_ms"),
    }

    log_router_decision(log_payload)

    target_agent = router_result.get("target_agent", "coach")
    fallback_to_coach = target_agent == "coach"
    current_state = context_payload.get("current_state")

    # ==================== EARLY HANDLING (OPTION C) ====================

    if target_agent == "plan" and current_state in ADAPTATION_FLOW_STATES:
        logger.info(
            "User %s in adaptation tunnel (state=%s), handling via tunnel prompt",
            user_id,
            current_state,
        )
        with SessionLocal() as db:
            try:
                reply_text, followups = await handle_adaptation_flow(
                    user_id,
                    message_text,
                    current_state,
                    db,
                )
                db.commit()
                return await _finalize_reply(reply_text, followup_messages=followups)
            except Exception as exc:
                db.rollback()
                logger.error(
                    "Adaptation flow failed for user %s in state %s: %s",
                    user_id,
                    current_state,
                    exc,
                    exc_info=True,
                )
                return await _finalize_reply(
                    "Щось пішло не так. Спробуй ще раз або напиши 'скасувати'."
                )

    if target_agent == "coach":
        worker_result = await _invoke_agent("coach", {"message_text": message_text})
        reply_text = str(worker_result.get("reply_text") or "")
        return await _finalize_reply(reply_text)

    if target_agent == "plan" and current_state not in ENTRY_PROMPT_ALLOWED_STATES:
        if current_state == "IDLE_NEW":
            return await _finalize_reply(
                "Спочатку пройди вітальний процес. Напиши 'почати'."
            )

        if current_state in ADAPTATION_ENTRY_STATES:
            logger.warning(
                "Plan agent signal in adaptation entry state %s bypassed to coach for user %s",
                current_state,
                user_id,
            )
        else:
            logger.warning(
                "Plan agent invoked from forbidden state %s for user %s, routing to coach",
                current_state,
                user_id,
            )
        coach_result = await _invoke_agent("coach", {"message_text": message_text})
        return await _finalize_reply(str(coach_result.get("reply_text") or ""))

    worker_payload = {
        "user_id": user_id,
        "router_result": router_result,
        **context_payload,
    }

    log_router_decision(
        {
            "event_type": "router_routing_decision",
            "timestamp": datetime.utcnow().isoformat(),
            "user_id": user_id,
            "target_agent": target_agent,
            "confidence": router_result.get("confidence"),
            "intent_bucket": router_result.get("intent_bucket"),
            "fallback_to_coach": fallback_to_coach,
            "router_result": router_result,
            "router_meta": router_meta,
        }
    )

    log_router_decision(
        {
            "event_type": "agent_invocation",
            "timestamp": datetime.utcnow().isoformat(),
            "agent_name": target_agent,
            "payload": worker_payload,
        }
    )

    worker_result = await _invoke_agent(target_agent, worker_payload)

    if target_agent == "plan" and isinstance(worker_result.get("tool_call"), dict):
        tool_result = run_plan_tool_call(worker_result["tool_call"])
        return await _finalize_reply(str(tool_result.get("user_text") or ""))

    if target_agent == "plan" and worker_result.get("transition_signal") == ADAPTATION_SELECTION:
        with SessionLocal() as db:
            active_plan = get_active_plan(db, user_id)
            if not active_plan:
                return await _finalize_reply(
                    "Спочатку потрібно створити план. Напиши 'створи план'."
                )
            try:
                await _commit_fsm_transition(
                    user_id=user_id,
                    target_agent=target_agent,
                    next_state=ADAPTATION_SELECTION,
                    db=db,
                    reason="user_initiated_adaptation",
                )
            except ValueError as exc:
                logger.error(
                    "FSM transition blocked for user %s: %s -> %s, reason: %s",
                    user_id,
                    context_payload.get("current_state"),
                    ADAPTATION_SELECTION,
                    exc,
                )
                return await _finalize_reply(
                    "Не можу розпочати адаптацію зараз. Спробуй пізніше або створи новий план."
                )

            reply_text, followups = await handle_adaptation_flow(
                user_id,
                message_text,
                ADAPTATION_SELECTION,
                db,
            )
            db.commit()

        return await _finalize_reply(reply_text, followup_messages=followups)

    # NOTE:
    # Auto-start PLAN_FLOW commits FSM transition eagerly and re-invokes Plan Agent.
    # The standard FSM transition logic below MUST NOT re-commit the same transition.
    auto_start_entry_states = PLAN_CREATION_ENTRY_STATES - {"ACTIVE_PAUSED"}
    if (
        target_agent == "plan"
        and context_payload.get("current_state") in auto_start_entry_states
        and worker_result.get("transition_signal") == "PLAN_FLOW:DATA_COLLECTION"
    ):
        transition_signal = worker_result.get("transition_signal")
        current_state = context_payload.get("current_state")
        if transition_signal == "PLAN_FLOW:DATA_COLLECTION":
            if current_state in {"ACTIVE", "ACTIVE_PAUSED", "ACTIVE_PAUSED_CONFIRMATION"}:
                did_drop = _auto_drop_plan_for_new_flow(user_id)
                if did_drop:
                    current_state = "IDLE_DROPPED"
                    context_payload["current_state"] = current_state
                else:
                    logger.warning(
                        "[FSM] Auto-drop failed, blocking PLAN_FLOW entry for user %s",
                        user_id,
                    )
                    transition_signal = None
        # TASK-4.3: PLAN_FLOW guard enforced via _guard_fsm_transition/can_transition.
        next_state, rejection_reason = _guard_fsm_transition(
            current_state,
            transition_signal,
            target_agent,
        )
        if transition_signal is not None and next_state is None:
            logger.warning(
                "[FSM] Ignoring transition_signal for user %s: %s (reason=%s, agent=%s)",
                user_id,
                transition_signal,
                rejection_reason or "invalid_state",
                target_agent,
            )
            log_metric(
                "fsm_transition_blocked",
                extra={
                    "user_id": user_id,
                    "agent": target_agent,
                    "current_state": current_state,
                    "transition_signal": transition_signal,
                    "reason": rejection_reason or "invalid_state",
                },
            )
        elif next_state is not None:
            previous_state = await _commit_fsm_transition(user_id, target_agent, next_state)
            if previous_state is not None:
                context_payload["current_state"] = next_state
                worker_payload["current_state"] = next_state
                if (
                    next_state == "PLAN_FLOW:DATA_COLLECTION"
                    and previous_state not in PLAN_FLOW_STATES
                ):
                    refreshed_parameters = normalize_plan_parameters(
                        await session_memory.get_plan_parameters(user_id)
                    )
                    context_payload["known_parameters"] = refreshed_parameters
                    worker_payload["known_parameters"] = refreshed_parameters
                log_router_decision(
                    {
                        "event_type": "agent_invocation",
                        "timestamp": datetime.utcnow().isoformat(),
                        "agent_name": target_agent,
                        "payload": worker_payload,
                    }
                )
                worker_result = await _invoke_agent(target_agent, worker_payload)

    if (
        context_payload.get("current_state") == "PLAN_FLOW:CONFIRMATION_PENDING"
        and target_agent == "plan"
        and worker_result.get("transition_signal") is None
        and worker_result.get("plan_updates") is None
        and not str(worker_result.get("reply_text") or "").strip()
    ):
        coach_result = await coach_agent(worker_payload)
        worker_result = coach_result
        target_agent = "coach"

    reply_text = str(worker_result.get("reply_text") or "")
    defer_draft = False
    plan_draft_parameters: Optional[Dict[str, Any]] = None

    current_state = context_payload.get("current_state")
    blocked_persistence_states = {"PLAN_FLOW:DATA_COLLECTION", "PLAN_FLOW:CONFIRMATION_PENDING"}

    error_payload = worker_result.get("error")
    if error_payload is not None:
        if error_payload.get("code") == "CONTRACT_MISMATCH":
            log_metric(
                "plan_contract_mismatch",
                extra={"user_id": user_id, "agent": target_agent},
            )
        log_router_decision(
            {
                "event_type": "plan_agent_error",
                "timestamp": datetime.utcnow().isoformat(),
                "user_id": user_id,
                "agent": target_agent,
                "error": error_payload,
            }
        )
        logger.warning(
            "[PLAN_AGENT] Error payload received for user %s (agent=%s): %s",
            user_id,
            target_agent,
            error_payload,
        )
        return await _finalize_reply(reply_text)

    plan_persisted = False
    generated_plan_object = worker_result.get("generated_plan_object")
    if generated_plan_object is not None and current_state not in blocked_persistence_states:
        with SessionLocal() as db:
            user: Optional[User] = db.query(User).filter(User.id == user_id).first()
            if not user:
                logger.warning(
                    "[PLAN] Generated plan ignored — user %s not found (agent=%s)",
                    user_id,
                    target_agent,
                )
                return await _finalize_reply(reply_text)
            try:
                _persist_generated_plan(db, user, generated_plan_object)
                db.commit()
            except (IntegrityError, PlanAgentEnvelopeError) as exc:
                db.rollback()
                logger.error(
                    "[PLAN] Failed to persist generated plan for user %s (agent=%s)",
                    user_id,
                    target_agent,
                    exc_info=exc,
                )
                log_metric(
                    "plan_validation_rejected",
                    extra={"user_id": user_id, "agent": target_agent},
                )
                fallback_text = _plan_agent_fallback_envelope().get("reply_text", "")
                return await _finalize_reply(fallback_text)
            else:
                logger.info(
                    "[PLAN] Generated plan persisted for user %s (agent=%s)",
                    user_id,
                    target_agent,
                )
                plan_persisted = True
                log_metric(
                    "plan_generated_ok",
                    extra={"user_id": user_id, "agent": target_agent},
                )
                if context_payload.get("current_state") in ADAPTATION_FLOW_STATES:
                    log_metric(
                        "adaptation_created",
                        extra={"user_id": user_id, "agent": target_agent},
                    )

    plan_updates = worker_result.get("plan_updates")
    draft_parameters = None
    if (
        target_agent == "plan"
        and isinstance(plan_updates, dict)
        and current_state in PLAN_FLOW_STATES
        and current_state != "PLAN_FLOW:DATA_COLLECTION"
    ):
        clean_updates = _sanitize_plan_updates(plan_updates)
        persistent_parameters = await session_memory.get_plan_parameters(user_id)
        updated_parameters = dict(persistent_parameters)
        if clean_updates:
            updated_parameters.update(clean_updates)
            await session_memory.set_plan_parameters(user_id, updated_parameters)
            context_payload["known_parameters"] = updated_parameters
            draft_parameters = updated_parameters
    transition_signal = worker_result.get("transition_signal")
    data_collection_notice: str | None = None
    if target_agent == "plan" and current_state == "PLAN_FLOW:DATA_COLLECTION":
        transition_signal = None
        persistent_parameters = normalize_plan_parameters(
            await session_memory.get_plan_parameters(user_id)
        )

        clean_updates: Dict[str, Any] = {}
        if isinstance(plan_updates, dict):
            clean_updates = _sanitize_plan_updates(plan_updates) or {}

        proposed_parameters = dict(persistent_parameters)
        if clean_updates:
            if "duration" in clean_updates:
                proposed_parameters["duration"] = clean_updates["duration"]
            if "focus" in clean_updates:
                proposed_parameters["focus"] = clean_updates["focus"]
            if "load" in clean_updates:
                proposed_parameters["load"] = clean_updates["load"]
                proposed_parameters.pop("preferred_time_slots", None)
            if "preferred_time_slots" in clean_updates:
                proposed_slots = [
                    slot for slot in clean_updates["preferred_time_slots"] if slot in PLAN_TIME_SLOT_VALUES
                ]
                proposed_parameters["preferred_time_slots"] = proposed_slots

        load = proposed_parameters.get("load")
        slots = proposed_parameters.get("preferred_time_slots") or []
        normalized_slots = [slot for slot in slots if slot in PLAN_TIME_SLOT_VALUES]

        if load == "INTENSIVE":
            proposed_parameters["preferred_time_slots"] = INTENSIVE_AUTO_SLOTS.copy()
            normalized_slots = INTENSIVE_AUTO_SLOTS.copy()
            data_collection_notice = (
                "Для інтенсивного плану автоматично призначено 3 слоти:\nMORNING / DAY / EVENING"
            )
        elif load == "MID" and "preferred_time_slots" in clean_updates and len(normalized_slots) != 2:
            params_to_save = {k: v for k, v in proposed_parameters.items() if k != "preferred_time_slots"}
            persistent_without_slots = {
                k: v for k, v in persistent_parameters.items() if k != "preferred_time_slots"
            }
            if params_to_save != persistent_without_slots:
                await session_memory.set_plan_parameters(user_id, params_to_save)
                context_payload["known_parameters"] = params_to_save
                draft_parameters = params_to_save
            reply_text = (
                "Для MID потрібно обрати рівно 2 часові слоти.\n\n"
                "Якщо хочеш 3 слоти — обери INTENSIVE.\n"
                "Якщо 1 слот — обери LITE.\n\n"
                "Які 2 часові слоти підходять?\n"
                "MORNING / DAY / EVENING"
            )
            return await _finalize_reply(reply_text)
        elif load == "LITE" and "preferred_time_slots" in clean_updates and len(normalized_slots) != 1:
            params_to_save = {k: v for k, v in proposed_parameters.items() if k != "preferred_time_slots"}
            persistent_without_slots = {
                k: v for k, v in persistent_parameters.items() if k != "preferred_time_slots"
            }
            if params_to_save != persistent_without_slots:
                await session_memory.set_plan_parameters(user_id, params_to_save)
                context_payload["known_parameters"] = params_to_save
                draft_parameters = params_to_save
            reply_text = (
                "Для LITE потрібно обрати рівно 1 часовий слот.\n\n"
                "Якщо хочеш 2 слоти — обери MID.\n"
                "Якщо 3 — обери INTENSIVE.\n\n"
                "Який часовий слот підходить?\n"
                "MORNING / DAY / EVENING"
            )
            return await _finalize_reply(reply_text)

        if proposed_parameters != persistent_parameters:
            await session_memory.set_plan_parameters(user_id, proposed_parameters)
            context_payload["known_parameters"] = proposed_parameters
            draft_parameters = proposed_parameters

        persistent_parameters = normalize_plan_parameters(
            await session_memory.get_plan_parameters(user_id)
        )
        duration = persistent_parameters.get("duration")
        focus = persistent_parameters.get("focus")
        load = persistent_parameters.get("load")
        slots = persistent_parameters.get("preferred_time_slots") or []
        normalized_slots = [slot for slot in slots if slot in PLAN_TIME_SLOT_VALUES]

        if duration is not None and focus is not None and load is not None:
            expected_slots = _expected_time_slots_for_load(load)
            if load == "MID" and len(normalized_slots) != 2:
                reply_text = (
                    "Для MID потрібно обрати рівно 2 часові слоти.\n\n"
                    "Якщо хочеш 3 слоти — обери INTENSIVE.\n"
                    "Якщо 1 слот — обери LITE.\n\n"
                    "Які 2 часові слоти підходять?\n"
                    "MORNING / DAY / EVENING"
                )
            elif load == "LITE" and len(normalized_slots) != 1:
                reply_text = (
                    "Для LITE потрібно обрати рівно 1 часовий слот.\n\n"
                    "Якщо хочеш 2 слоти — обери MID.\n"
                    "Якщо 3 — обери INTENSIVE.\n\n"
                    "Який часовий слот підходить?\n"
                    "MORNING / DAY / EVENING"
                )
            elif load == "INTENSIVE":
                if normalized_slots != INTENSIVE_AUTO_SLOTS:
                    persistent_parameters["preferred_time_slots"] = INTENSIVE_AUTO_SLOTS.copy()
                    await session_memory.set_plan_parameters(user_id, persistent_parameters)
                    context_payload["known_parameters"] = persistent_parameters
                    draft_parameters = persistent_parameters
                    normalized_slots = INTENSIVE_AUTO_SLOTS.copy()
                    reply_text = (
                        "В інтенсивному режимі завжди використовується 3 часові слоти:\nMORNING / DAY / EVENING"
                    )
                transition_signal = "PLAN_FLOW:CONFIRMATION_PENDING"
            elif expected_slots is not None and len(normalized_slots) == expected_slots:
                transition_signal = "PLAN_FLOW:CONFIRMATION_PENDING"
    if target_agent == "plan" and current_state == "PLAN_FLOW:CONFIRMATION_PENDING":
        confirmation_reply = await handle_confirmation_pending_action(
            user_id,
            plan_updates,
            transition_signal,
            reply_text,
            context_payload,
        )
        normalized_reply = _normalize_confirmation_reply(confirmation_reply)
        if normalized_reply is not None:
            return await _finalize_reply(**normalized_reply)
        if transition_signal == "PLAN_FLOW:FINALIZATION":
            lock_key = f"finalization_lock:{user_id}"
            already_running = True
            if redis_client is not None:
                try:
                    already_running = bool(await redis_client.set(lock_key, "1", nx=True, ex=30))
                except Exception as exc:
                    logger.warning("[PLAN_FINALIZATION] Failed to acquire redis lock for user %s: %s", user_id, exc)
                    already_running = True
            if not already_running:
                return await _finalize_reply("⏳ Plan is already being activated, please wait.")

            async def _run_finalization() -> None:
                try:
                    with SessionLocal.begin() as db:
                        draft = validate_for_finalization(db, user_id)
                        activation_time_utc = datetime.now(timezone.utc)
                        plan = finalize_plan(
                            db,
                            user_id,
                            draft,
                            activation_time_utc=activation_time_utc,
                        )
                        current_parameters = normalize_plan_parameters(
                            await session_memory.get_plan_parameters(user_id)
                        )
                        active_plan = get_active_plan(db, user_id)
                        _slots = (current_parameters or {}).get("preferred_time_slots") or []
                        if _slots and active_plan:
                            active_plan.preferred_time_slots = [
                                s for s in ["MORNING", "DAY", "EVENING"] if s in _slots
                            ]
                    await asyncio.to_thread(activate_plan_side_effects, plan.id, user_id)
                    log_metric("plan_finalized", extra={"user_id": user_id, "plan_id": plan.id})
                    await _commit_fsm_transition(user_id, "plan", "ACTIVE")
                    selected_slots = [
                        slot
                        for slot in (current_parameters.get("preferred_time_slots") or [])
                        if slot in PLAN_TIME_SLOT_VALUES
                    ]
                    await session_memory.clear_plan_parameters(user_id)
                    try:
                        from app.telegram import bot as tg_bot
                        with SessionLocal() as db:
                            user = db.query(User).filter(User.id == user_id).first()
                            tg_id = user.tg_id if user else None
                        if tg_id:
                            activation_msg = (
                                "✅ План активовано.\n"
                                "Перші завдання надійдуть згідно з обраними часовими слотами."
                            )
                            activation_info = build_activation_info_message(selected_slots, None)
                            await tg_bot.send_message(tg_id, f"{activation_msg}\n\n{activation_info}")
                    except Exception as notify_exc:
                        logger.error(
                            "[PLAN_FINALIZATION] Failed to send activation confirmation to user %s: %s",
                            user_id,
                            notify_exc,
                        )
                except (
                    DraftNotFoundError,
                    InvalidDraftError,
                    ActivePlanExistsError,
                    FinalizationError,
                ) as exc:
                    logger.error(
                        "[PLAN_FINALIZATION] Failed to finalize plan for user %s: %s",
                        user_id,
                        exc,
                    )
                    try:
                        from app.telegram import bot as tg_bot
                        with SessionLocal() as db:
                            user = db.query(User).filter(User.id == user_id).first()
                            tg_id = user.tg_id if user else None
                        if tg_id:
                            await tg_bot.send_message(tg_id, PLAN_FINALIZATION_ERROR_MESSAGE)
                    except Exception as send_exc:
                        logger.error(
                            "[PLAN_FINALIZATION] Failed to send finalization error to user %s: %s",
                            user_id,
                            send_exc,
                        )
                finally:
                    if redis_client is not None:
                        try:
                            await redis_client.delete(lock_key)
                        except Exception as exc:
                            logger.warning(
                                "[PLAN_FINALIZATION] Failed to release redis lock for user %s: %s",
                                user_id,
                                exc,
                            )

            asyncio.create_task(_run_finalization())
            return await _finalize_reply("⏳ План генерується…")
    if (
        plan_updates
        and isinstance(plan_updates, dict)
        and current_state not in blocked_persistence_states
    ):
        allowed_execution_adaptations = {"pause", "resume", "PAUSE_PLAN", "RESUME_PLAN"}
        should_persist_updates = bool(generated_plan_object) or (
            plan_updates.get("adaptation_type") in allowed_execution_adaptations
        )
        if not should_persist_updates:
            logger.info(
                "[PLAN] Skipping plan updates outside allowed persistence window (user=%s, agent=%s, state=%s)",
                user_id,
                target_agent,
                current_state,
            )
        elif "adaptation_type" in plan_updates:
            if plan_updates.get("adaptation_type") not in allowed_execution_adaptations:
                logger.info(
                    "[PLAN] Skipping non-execution adaptation type %s for user %s (agent=%s)",
                    plan_updates.get("adaptation_type"),
                    user_id,
                    target_agent,
                )
                return await _finalize_reply(reply_text)
            adaptation_result = None
            with SessionLocal() as db:
                user: Optional[User] = db.query(User).filter(User.id == user_id).first()
                if not user:
                    logger.warning(
                        "[PLAN] Adaptation ignored — user %s not found (agent=%s)",
                        user_id,
                        target_agent,
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
                        "[PLAN] Adaptation ignored — active plan missing (user=%s, agent=%s)",
                        user_id,
                        target_agent,
                    )
                    return await _finalize_reply(reply_text)
                try:
                    adaptation_result = apply_plan_adaptation(db, active_plan.id, plan_updates)
                    db.commit()
                except (PlanAdaptationError, IntegrityError) as exc:
                    db.rollback()
                    logger.error(
                        "[PLAN] Failed to apply adaptation for user %s (agent=%s): %s",
                        user_id,
                        target_agent,
                        exc,
                    )
                    log_metric(
                        "plan_adaptation_failed",
                        extra={
                            "user_id": user_id,
                            "agent": target_agent,
                            "adaptation_type": plan_updates.get("adaptation_type"),
                        },
                    )
                else:
                    log_metric(
                        "plan_adaptation_applied",
                        extra={
                            "user_id": user_id,
                            "agent": target_agent,
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
                        "[PLAN] Updates ignored — user %s not found (agent=%s)",
                        user_id,
                        target_agent,
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
                        "[PLAN] Failed to persist updates for user %s (agent=%s)",
                        user_id,
                        target_agent,
                    )
                else:
                    logger.info(
                        "[PLAN] User %s updated: end=%s",
                        user_id,
                        user.plan_end_date,
                    )

    if transition_signal == "PLAN_FLOW:CONFIRMATION_PENDING":
        parameters_for_draft = draft_parameters or (context_payload.get("known_parameters") or {})
        if defer_plan_draft:
            defer_draft = True
            plan_draft_parameters = parameters_for_draft
            reply_text = data_collection_notice if data_collection_notice else ""
            show_plan_actions = True
        else:
            preview_text = await build_plan_draft_preview(user_id, parameters_for_draft)
            if preview_text == PLAN_GENERATION_ERROR_MESSAGE:
                transition_signal = None
                reply_text = preview_text
            else:
                show_plan_actions = True
                if data_collection_notice:
                    reply_text = f"{data_collection_notice}\n\n{preview_text}"
                else:
                    reply_text = preview_text
    if transition_signal == "PLAN_FLOW:DATA_COLLECTION":
        if context_payload.get("current_state") in {"ACTIVE", "ACTIVE_PAUSED", "ACTIVE_PAUSED_CONFIRMATION"}:
            did_drop = _auto_drop_plan_for_new_flow(user_id)
            if did_drop:
                context_payload["current_state"] = "IDLE_DROPPED"
            else:
                logger.warning(
                    "[FSM] Auto-drop failed, blocking PLAN_FLOW entry for user %s",
                    user_id,
                )
                transition_signal = None
    # TASK-4.3: PLAN_FLOW guard enforced via _guard_fsm_transition/can_transition.
    next_state, rejection_reason = _guard_fsm_transition(
        context_payload.get("current_state"),
        transition_signal,
        target_agent,
        plan_persisted=plan_persisted,
    )
    if transition_signal is not None and next_state is None:
        logger.warning(
            "[FSM] Ignoring transition_signal for user %s: %s (reason=%s, agent=%s)",
            user_id,
            transition_signal,
            rejection_reason or "invalid_state",
            target_agent,
        )
        log_metric(
            "fsm_transition_blocked",
            extra={
                "user_id": user_id,
                "agent": target_agent,
                "current_state": context_payload.get("current_state"),
                "transition_signal": transition_signal,
                "reason": rejection_reason or "invalid_state",
            },
        )
        defer_draft = False
        plan_draft_parameters = None
    elif next_state is not None:
        previous_state = await _commit_fsm_transition(user_id, target_agent, next_state)
        if previous_state is None:
            return await _finalize_reply(reply_text)
        if next_state == "PLAN_FLOW:DATA_COLLECTION" and previous_state in PLAN_FLOW_STATES:
            refreshed_parameters = normalize_plan_parameters(
                await session_memory.get_plan_parameters(user_id)
            )
            context_payload["current_state"] = next_state
            worker_payload["current_state"] = next_state
            context_payload["known_parameters"] = refreshed_parameters
            worker_payload["known_parameters"] = refreshed_parameters
            log_router_decision(
                {
                    "event_type": "agent_invocation",
                    "timestamp": datetime.utcnow().isoformat(),
                    "agent_name": target_agent,
                    "payload": worker_payload,
                }
            )
            worker_result = await _invoke_agent(target_agent, worker_payload)
            reply_text = str(worker_result.get("reply_text") or "")
            return await _finalize_reply(
                reply_text,
                defer_draft=defer_draft,
                plan_draft_parameters=plan_draft_parameters,
                show_plan_actions=show_plan_actions,
            )

    return await _finalize_reply(
        reply_text,
        defer_draft=defer_draft,
        plan_draft_parameters=plan_draft_parameters,
        show_plan_actions=show_plan_actions,
    )


async def _invoke_agent(target_agent: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if target_agent == "safety":
        return await mock_safety_agent(payload)
    if target_agent == "onboarding":
        return await mock_onboarding_agent(payload)
    if target_agent == "manager":
        return await mock_manager_agent(payload)
    if target_agent == "plan":
        try:
            return await plan_agent(payload)
        except Exception as exc:
            user_id = payload.get("user_id")
            log_router_decision(
                {
                    "event_type": "plan_agent_error",
                    "timestamp": datetime.utcnow().isoformat(),
                    "user_id": user_id,
                    "error": str(exc),
                }
            )
            logger.error(
                "[PLAN_AGENT] Error during plan agent call for user %s",
                user_id,
                exc_info=exc,
            )
            return _plan_agent_fallback_envelope()
    return await coach_agent(payload)
