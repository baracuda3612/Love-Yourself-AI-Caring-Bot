from datetime import datetime
from typing import Any, Dict, List, Optional

import logging

import pytz
from sqlalchemy.orm import Session

from app.ai_router import cognitive_route_message
from app.db import ChatHistory, SessionLocal, User, UserEvent, UserProfile
from app.logic.rule_engine import RuleEngine
from app.logging.router_logging import log_router_decision
from app.session_memory import SessionMemory
from app.workers.coach_agent import coach_agent
from app.workers.mock_workers import (
    mock_manager_agent,
    mock_onboarding_agent,
    mock_plan_agent,
    mock_safety_agent,
)

session_memory = SessionMemory(limit=20)
logger = logging.getLogger(__name__)


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

    router_input = {
        "user_id": user_id,
        "message_text": context_payload.get("message_text", message_text),
        "short_term_history": context_payload.get("short_term_history"),
        "profile_snapshot": context_payload.get("profile_snapshot"),
        "current_state": context_payload.get("current_state"),
        "temporal_context": context_payload.get("temporal_context"),
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

    context_payload = await build_user_context(user_id, message_text)
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

    return reply_text
async def _invoke_agent(target_agent: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if target_agent == "safety":
        return await mock_safety_agent(payload)
    if target_agent == "onboarding":
        return await mock_onboarding_agent(payload)
    if target_agent == "manager":
        return await mock_manager_agent(payload)
    if target_agent == "plan":
        return await mock_plan_agent(payload)
    return await coach_agent(payload)
