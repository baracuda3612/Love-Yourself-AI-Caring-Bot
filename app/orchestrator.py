from datetime import datetime
from typing import Any, Dict, List, Optional

from app.ai_router import cognitive_route_message
from app.db import ChatHistory, SessionLocal, SenderRole, User, UserProfile
from app.logging.router_logging import log_router_decision
from app.workers.mock_workers import (
    mock_coach_agent,
    mock_manager_agent,
    mock_onboarding_agent,
    mock_plan_agent,
    mock_safety_agent,
)


async def get_stm_history(user_id: int) -> List[Dict[str, str]]:
    """Short-term memory: останні 10 повідомлень користувача та бота."""
    with SessionLocal() as db:
        rows = (
            db.query(ChatHistory)
            .filter(ChatHistory.user_id == user_id)
            .order_by(ChatHistory.created_at.desc())
            .limit(10)
            .all()
        )

    history = list(reversed(rows))
    return [
        {"role": row.role.value if isinstance(row.role, SenderRole) else str(row.role), "content": row.content}
        for row in history
    ]


async def get_ltm_snapshot(user_id: int) -> Dict[str, Any]:
    """Long-term snapshot: поля профілю користувача."""
    with SessionLocal() as db:
        profile: Optional[UserProfile] = (
            db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
        )

    if not profile:
        return {}

    return {
        "main_goal": profile.main_goal,
        "communication_style": profile.communication_style,
        "name_preference": profile.name_preference,
    }


async def get_fsm_state(user_id: int) -> Optional[str]:
    """Повертає поточний FSM-стан користувача."""
    with SessionLocal() as db:
        user: Optional[User] = db.query(User).filter(User.id == user_id).first()

    return user.current_state if user else None


async def call_router(user_id: int, message_text: str) -> Dict[str, Any]:
    """
    Сервісний хелпер: збирає контекст, формує payload для Router'а,
    викликає router і повертає JSON-відповідь (target_agent, priority, agent_instruction).
    """

    stm_history = await get_stm_history(user_id)
    ltm_snapshot = await get_ltm_snapshot(user_id)
    fsm_state = await get_fsm_state(user_id)

    router_input = {
        "user_id": user_id,
        "message_text": message_text,
        "short_term_history": stm_history,
        "profile_snapshot": ltm_snapshot,
        "current_state": fsm_state,
    }

    router_output = await cognitive_route_message(router_input)
    return {
        "router_result": router_output.get("router_result", {}),
        "router_meta": router_output.get("router_meta", {}),
        "fsm_state": fsm_state,
        "session_id": None,
        "input_message": message_text,
    }


async def handle_incoming_message(user_id: int, message_text: str) -> str:
    """
    Головний оркестратор:
    - збирає контекст
    - викликає Router
    - за target_agent викликає відповідний mock-агент
    - повертає text-відповідь для користувача
    """

    router_output = await call_router(user_id, message_text)

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
        "agent_instruction": router_result.get("agent_instruction"),
        "llm_prompt_tokens": router_meta.get("llm_prompt_tokens"),
        "llm_response_tokens": router_meta.get("llm_response_tokens"),
        "router_latency_ms": router_meta.get("router_latency_ms"),
    }

    log_router_decision(log_payload)

    target_agent = router_result.get("target_agent") or "coach"
    worker_payload = {
        "user_id": user_id,
        "message_text": message_text,
        "agent_instruction": router_result.get("agent_instruction"),
        "priority": router_result.get("priority"),
        "router_result": router_result,
    }

    if target_agent == "safety":
        worker_result = await mock_safety_agent(worker_payload)
    elif target_agent == "onboarding":
        worker_result = await mock_onboarding_agent(worker_payload)
    elif target_agent == "manager":
        worker_result = await mock_manager_agent(worker_payload)
    elif target_agent == "plan":
        worker_result = await mock_plan_agent(worker_payload)
    else:
        worker_result = await mock_coach_agent(worker_payload)

    return str(worker_result.get("reply_text") or "")
