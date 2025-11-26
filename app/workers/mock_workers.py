from typing import Any, Dict, Optional


async def mock_safety_agent(
    user_id: int,
    message_text: str,
    agent_instruction: Optional[str] = None,
    priority: Optional[float] = None,
    router_result: Optional[Dict[str, Any]] = None,
) -> str:
    return "[safety] Please stay safe. We are here for you."


async def mock_onboarding_agent(
    user_id: int,
    message_text: str,
    agent_instruction: Optional[str] = None,
    priority: Optional[float] = None,
    router_result: Optional[Dict[str, Any]] = None,
) -> str:
    return "[onboarding] Let's get you set up."


async def mock_manager_agent(
    user_id: int,
    message_text: str,
    agent_instruction: Optional[str] = None,
    priority: Optional[float] = None,
    router_result: Optional[Dict[str, Any]] = None,
) -> str:
    return "[manager] Managing your preferences."


async def mock_plan_agent(
    user_id: int,
    message_text: str,
    agent_instruction: Optional[str] = None,
    priority: Optional[float] = None,
    router_result: Optional[Dict[str, Any]] = None,
) -> str:
    return "[plan] Let's plan your next steps."


async def mock_coach_agent(
    user_id: int,
    message_text: str,
    agent_instruction: Optional[str] = None,
    priority: Optional[float] = None,
    router_result: Optional[Dict[str, Any]] = None,
) -> str:
    return "[coach] I'm here to listen."
