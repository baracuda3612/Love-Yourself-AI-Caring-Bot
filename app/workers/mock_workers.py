import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def _log_agent_call(agent_name: str, payload: Dict[str, Any]) -> None:
    user_id = payload.get("user_id")
    agent_instruction = payload.get("agent_instruction")

    logger.info("Called %s for user %s", agent_name, user_id)
    logger.info("Agent instruction: %s", str(agent_instruction))


def _build_response(agent_name: str, reply_text: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "agent_name": agent_name,
        "reply_type": "text",
        "reply_text": reply_text,
        "debug": {
            "agent_instruction": payload.get("agent_instruction"),
            "note": "This is a mock agent response."
        },
    }


async def mock_safety_agent(payload: Dict[str, Any]) -> Dict[str, Any]:
    _log_agent_call("mock_safety_agent", payload)

    return _build_response(
        agent_name="mock_safety_agent",
        reply_text="Я мок-безпековий агент. Все під контролем. Це тестова відповідь.",
        payload=payload,
    )


async def mock_onboarding_agent(payload: Dict[str, Any]) -> Dict[str, Any]:
    _log_agent_call("mock_onboarding_agent", payload)

    return _build_response(
        agent_name="mock_onboarding_agent",
        reply_text="Я мок-онбординг агент. Нібито питаю тебе про цілі, але це тест.",
        payload=payload,
    )


async def mock_manager_agent(payload: Dict[str, Any]) -> Dict[str, Any]:
    _log_agent_call("mock_manager_agent", payload)

    return _build_response(
        agent_name="mock_manager_agent",
        reply_text="Я мок-менеджер. Зараз би обговорював з тобою корпоративні умови, але це тест.",
        payload=payload,
    )


async def mock_plan_agent(payload: Dict[str, Any]) -> Dict[str, Any]:
    _log_agent_call("mock_plan_agent", payload)

    return _build_response(
        agent_name="mock_plan_agent",
        reply_text="Я мок-план агент. Мав би будувати психоплан, але віддаю тестову відповідь.",
        payload=payload,
    )


async def mock_coach_agent(payload: Dict[str, Any]) -> Dict[str, Any]:
    _log_agent_call("mock_coach_agent", payload)

    return _build_response(
        agent_name="mock_coach_agent",
        reply_text="Я мок-коуч агент. Тут мало б бути щось мудре і підтримуюче, але це тест.",
        payload=payload,
    )


MOCK_AGENTS: Dict[str, Any] = {
    "mock_safety_agent": mock_safety_agent,
    "mock_onboarding_agent": mock_onboarding_agent,
    "mock_manager_agent": mock_manager_agent,
    "mock_plan_agent": mock_plan_agent,
    "mock_coach_agent": mock_coach_agent,
}
