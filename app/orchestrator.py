from datetime import datetime, timedelta
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
    PlanInstance,
    SessionLocal,
    User,
    UserEvent,
    UserProfile,
)
from app.logic.rule_engine import RuleEngine
from app.logging.router_logging import log_router_decision
from app.session_memory import SessionMemory
from app.workers.coach_agent import coach_agent
from app.workers.mock_workers import (
    mock_manager_agent,
    mock_onboarding_agent,
    mock_safety_agent,
)
from app.schemas.planner import GeneratedPlan

session_memory = SessionMemory(limit=20)
logger = logging.getLogger(__name__)

ALLOWED_BASE_STATES = {
    "IDLE_ONBOARDED",
    "IDLE_PLAN_ABORTED",
    "IDLE_FINISHED",
    "IDLE_DROPPED",
    "ACTIVE",
    "ACTIVE_CONFIRMATION",
    "ADAPTATION_FLOW",
}
PREFIXED_STATES = {"PLAN_FLOW"}


def _violates_tunnel_exit(current_state: Optional[str], new_state: str) -> bool:
    if not current_state:
        return False
    current_state = current_state.strip()
    if not current_state:
        return False
    if current_state.startswith("PLAN_FLOW") and not (
        new_state.startswith("PLAN_FLOW") or new_state in {"ACTIVE", "IDLE_PLAN_ABORTED"}
    ):
        return True
    return False


def _normalize_fsm_state(raw_state: Optional[str], current_state: Optional[str] = None) -> Optional[str]:
    if not raw_state:
        return None

    state = raw_state.strip()
    if not state:
        return None

    if ":" in state:
        prefix, suffix = state.split(":", 1)
    else:
        prefix, suffix = state, None

    prefix = prefix.upper()
    normalized = prefix if suffix is None else f"{prefix}:{suffix}"

    if suffix is None:
        if normalized not in ALLOWED_BASE_STATES:
            return None
    elif prefix not in PREFIXED_STATES:
        return None

    if _violates_tunnel_exit(current_state, normalized):
        return None

    return normalized


def _get_fsm_rejection_reason(raw_state: Any, current_state: Optional[str] = None) -> Optional[str]:
    if raw_state is None:
        return "missing"
    if not isinstance(raw_state, str):
        return "non_string"
    state = raw_state.strip()
    if not state:
        return "empty"
    if ":" in state:
        prefix, suffix = state.split(":", 1)
        prefix = prefix.upper()
        if prefix not in PREFIXED_STATES:
            return "invalid_prefix"
        normalized = f"{prefix}:{suffix}"
        if _violates_tunnel_exit(current_state, normalized):
            return "tunnel_exit_rejected"
        return None
    if state.upper() not in ALLOWED_BASE_STATES:
        return "not_allowed"
    if _violates_tunnel_exit(current_state, state.upper()):
        return "tunnel_exit_rejected"
    return None


def _is_forbidden_transition(previous_state: Optional[str], next_state: str) -> bool:
    if not previous_state:
        return False
    if previous_state.startswith("PLAN_FLOW") and next_state == "IDLE_FINISHED":
        return True
    if previous_state.startswith("PLAN_FLOW") and next_state.startswith("PLAN_FLOW"):
        allowed_plan_flow = {
            ("PLAN_FLOW:DATA_COLLECTION", "PLAN_FLOW:CONFIRMATION_PENDING"),
            ("PLAN_FLOW:CONFIRMATION_PENDING", "PLAN_FLOW:FINALIZATION"),
        }
        if previous_state != next_state and (previous_state, next_state) not in allowed_plan_flow:
            return True
    if previous_state.startswith("PLAN_FLOW") and next_state == "ACTIVE":
        return previous_state != "PLAN_FLOW:FINALIZATION"
    if next_state == "IDLE_PLAN_ABORTED" and not previous_state.startswith("PLAN_FLOW"):
        return previous_state != next_state
    if previous_state == "IDLE_ONBOARDED" and next_state == "ACTIVE":
        return True
    if next_state == "ACTIVE_CONFIRMATION" and previous_state != "ADAPTATION_FLOW":
        return True
    return False


def _has_complete_plan_metadata(user: User) -> bool:
    return bool(user.plan_end_date and user.current_load and user.execution_policy)


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


def _get_skip_streak(db: Session, user_id: int) -> int:
    failure_event_types = {"task_skipped", "task_ignored", "task_failed"}
    events = (
        db.query(UserEvent.event_type)
        .filter(UserEvent.user_id == user_id)
        .order_by(UserEvent.timestamp.desc())
        .limit(RuleEngine.MAX_SKIP_THRESHOLD)
        .all()
    )

    skip_streak = 0
    for (event_type,) in events:
        if event_type not in failure_event_types:
            break
        skip_streak += 1

    return skip_streak


def _evaluate_adaptation_signal(user_id: int) -> Optional[str]:
    with SessionLocal() as db:
        user: Optional[User] = db.query(User).filter(User.id == user_id).first()
        if not user:
            return None
        if user.current_state != "ACTIVE":
            return None

        skip_streak = _get_skip_streak(db, user_id)

    return RuleEngine().evaluate(current_load=user.current_load, skip_streak=skip_streak)


def _inject_adaptation_metadata(context_payload: Dict[str, Any], signal: str) -> None:
    planner_context = context_payload.get("planner_context")
    if not isinstance(planner_context, dict):
        planner_context = {}
    planner_context["ADAPTATION_METADATA"] = (
        f"System suggests: {signal}. (Use this info only if contextually appropriate, do not spam)"
    )
    context_payload["planner_context"] = planner_context


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
    adaptation_version = (latest_plan.adaptation_version + 1) if latest_plan else 1
    if latest_plan and latest_plan.status == "active":
        latest_plan.status = "abandoned"

    ai_plan = AIPlan(
        user_id=user.id,
        title=parsed_plan.title,
        module_id=parsed_plan.module_id,
        goal_description=parsed_plan.reasoning,
        status="active",
        adaptation_version=adaptation_version,
    )
    db.add(ai_plan)
    db.flush()

    for day in parsed_plan.schedule:
        day_record = AIPlanDay(
            plan_id=ai_plan.id,
            day_number=day.day_number,
            focus_theme=day.focus_theme,
        )
        db.add(day_record)
        db.flush()
        for index, step in enumerate(day.steps):
            db.add(
                AIPlanStep(
                    day_id=day_record.id,
                    title=step.title,
                    description=step.description,
                    step_type=step.step_type,
                    difficulty=step.difficulty,
                    order_in_day=index,
                    time_of_day=step.time_of_day,
                )
            )

    db.add(
        PlanInstance(
            user_id=user.id,
            blueprint_id=str(parsed_plan.module_id),
            initial_parameters=plan_payload,
        )
    )

    tz = _safe_timezone(user.timezone)
    user.plan_end_date = _derive_plan_end_date(parsed_plan, tz)
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

    return {
        "message_text": message_text,
        "short_term_history": stm_history,
        "profile_snapshot": ltm_snapshot,
        "current_state": fsm_state,
        "temporal_context": temporal_context,
    }


async def call_router(user_id: int, message_text: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Сервісний хелпер: збирає контекст, формує payload для Router'а,
    викликає router і повертає JSON-відповідь (target_agent, priority).
    """

    context_payload = context or await build_user_context(user_id, message_text)

    # STRICT: Router only reads user_id, current_state, latest_user_message, short_term_history
    router_input = {
        "user_id": user_id,
        "latest_user_message": context_payload.get("message_text", message_text),
        "short_term_history": context_payload.get("short_term_history"),
        "current_state": context_payload.get("current_state"),
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


async def handle_incoming_message(user_id: int, message_text: str) -> str:
    """
    Головний оркестратор:
    - збирає контекст
    - викликає Router
    - за target_agent викликає відповідний mock-агент
    - повертає text-відповідь для користувача
    """

    await session_memory.append_message(user_id, "user", message_text)

    _auto_complete_plan_if_needed(user_id)

    context_payload = await build_user_context(user_id, message_text)
    router_output = await call_router(user_id, message_text, context_payload)

    router_result = router_output.get("router_result", {})
    router_meta = router_output.get("router_meta", {})

    current_state = context_payload.get("current_state") or ""
    forced_agent: Optional[str] = None
    if current_state.startswith("PLAN_FLOW") or current_state == "ADAPTATION_FLOW":
        forced_agent = "plan"
    elif current_state.startswith("ONBOARDING"):
        forced_agent = "onboarding"

    if router_result.get("target_agent") == "safety":
        forced_agent = None

    if forced_agent:
        router_result["target_agent"] = forced_agent

    log_payload = {
        "event_type": "router_decision",
        "timestamp": datetime.utcnow().isoformat(),
        "user_id": user_id,
        "session_id": router_output.get("session_id"),
        "input_message": router_output.get("input_message", message_text),
        "fsm_state": router_output.get("fsm_state"),
        "target_agent": router_result.get("target_agent"),
        "priority": router_result.get("priority"),
        "llm_prompt_tokens": router_meta.get("llm_prompt_tokens"),
        "llm_response_tokens": router_meta.get("llm_response_tokens"),
        "router_latency_ms": router_meta.get("router_latency_ms"),
    }

    log_router_decision(log_payload)

    target_agent = router_result.get("target_agent") or "coach"
    fallback_to_coach = router_result.get("target_agent") is None

    if context_payload.get("current_state") == "ACTIVE":
        signal = _evaluate_adaptation_signal(user_id)
        if signal:
            _inject_adaptation_metadata(context_payload, signal)

    worker_payload = {
        "user_id": user_id,
        "priority": router_result.get("priority"),
        "router_result": router_result,
        **context_payload,
    }

    log_router_decision(
        {
            "event_type": "router_routing_decision",
            "timestamp": datetime.utcnow().isoformat(),
            "user_id": user_id,
            "target_agent": target_agent,
            "priority": router_result.get("priority"),
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

    reply_text = str(worker_result.get("reply_text") or "")
    await session_memory.append_message(user_id, "assistant", reply_text)

    error_payload = worker_result.get("error")
    if error_payload is not None:
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
        return reply_text

    generated_plan_object = worker_result.get("generated_plan_object")
    if generated_plan_object is not None:
        with SessionLocal() as db:
            user: Optional[User] = db.query(User).filter(User.id == user_id).first()
            if not user:
                logger.warning(
                    "[PLAN] Generated plan ignored — user %s not found (agent=%s)",
                    user_id,
                    target_agent,
                )
                return reply_text
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
                raise
            else:
                logger.info(
                    "[PLAN] Generated plan persisted for user %s (agent=%s)",
                    user_id,
                    target_agent,
                )

    plan_updates = worker_result.get("plan_updates")
    if plan_updates and isinstance(plan_updates, dict):
        with SessionLocal() as db:
            user: Optional[User] = db.query(User).filter(User.id == user_id).first()
            if not user:
                logger.warning(
                    "[PLAN] Updates ignored — user %s not found (agent=%s)",
                    user_id,
                    target_agent,
                )
                return reply_text
            try:
                if "plan_end_date" in plan_updates:
                    raw_end_date = plan_updates.get("plan_end_date")
                    if raw_end_date:
                        user.plan_end_date = datetime.fromisoformat(str(raw_end_date))
                    else:
                        user.plan_end_date = None
                if "current_load" in plan_updates:
                    user.current_load = plan_updates.get("current_load")
                if "execution_policy" in plan_updates:
                    user.execution_policy = plan_updates.get("execution_policy")
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
                    "[PLAN] User %s updated: end=%s load=%s policy=%s",
                    user_id,
                    user.plan_end_date,
                    user.current_load,
                    user.execution_policy,
                )

    transition_signal = worker_result.get("transition_signal")
    effective_signal = transition_signal
    if transition_signal == "ACTIVE_CONFIRMATION":
        effective_signal = "ACTIVE"
    normalized_state = (
        _normalize_fsm_state(effective_signal, current_state=context_payload.get("current_state"))
        if isinstance(effective_signal, str)
        else None
    )
    if transition_signal is not None and normalized_state is None:
        reason = (
            _get_fsm_rejection_reason(
                transition_signal,
                current_state=context_payload.get("current_state"),
            )
            or "invalid_state"
        )
        logger.warning(
            "[FSM] Ignoring transition_signal for user %s: %s (reason=%s, agent=%s)",
            user_id,
            transition_signal,
            reason,
            target_agent,
        )
    elif normalized_state is not None:
        previous_state: Optional[str] = None
        did_commit = False
        with SessionLocal() as db:
            user: Optional[User] = db.query(User).filter(User.id == user_id).first()
            if not user:
                logger.warning(
                    "[FSM] transition_signal ignored — user %s not found (agent=%s)",
                    user_id,
                    target_agent,
                )
                return reply_text
            previous_state = user.current_state
            if _is_forbidden_transition(previous_state, normalized_state):
                logger.warning(
                    "[FSM] Forbidden transition ignored for user %s: %s → %s (agent=%s)",
                    user_id,
                    previous_state,
                    normalized_state,
                    target_agent,
                )
                return reply_text
            if normalized_state == "ACTIVE" and previous_state == "IDLE_FINISHED":
                logger.warning(
                    "[FSM] Restore blocked — plan is fully finished for user %s (agent=%s)",
                    user_id,
                    target_agent,
                )
                return reply_text
            if normalized_state == "ACTIVE" and previous_state in {
                "IDLE_DROPPED",
                "ACTIVE_PAUSED",
                "IDLE_PLAN_ABORTED",
            }:
                user.execution_policy = "EXECUTION"
            if normalized_state == "ACTIVE" and previous_state.startswith("PLAN_FLOW"):
                if not _has_complete_plan_metadata(user):
                    logger.warning("[FSM] Transition rejected — incomplete plan metadata")
                    return reply_text
            user.current_state = normalized_state
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                logger.error(
                    "[FSM] Failed to persist transition for user %s: %s (agent=%s)",
                    user_id,
                    normalized_state,
                    target_agent,
                )
            else:
                did_commit = True

        if did_commit and previous_state is not None:
            logger.info(
                "[FSM] User %s state transition: %s → %s (agent=%s)",
                user_id,
                previous_state,
                normalized_state,
                target_agent,
            )
            log_router_decision(
                {
                    "event_type": "fsm_transition",
                    "user_id": user_id,
                    "agent": target_agent,
                    "from_state": previous_state,
                    "to_state": normalized_state,
                }
            )

    return reply_text


async def _invoke_agent(target_agent: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if target_agent == "safety":
        return await mock_safety_agent(payload)
    if target_agent == "onboarding":
        return await mock_onboarding_agent(payload)
    if target_agent == "manager":
        return await mock_manager_agent(payload)
    if target_agent == "plan":
        return await plan_agent(payload)
    return await coach_agent(payload)
