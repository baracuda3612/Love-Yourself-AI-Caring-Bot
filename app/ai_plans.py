"""LLM-driven plan agent utilities for tool calling."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from app.ai import async_client, extract_output_text
from app.config import settings

__all__ = [
    "PlanAgentEnvelopeError",
    "generate_plan_agent_response",
    "plan_agent",
    "run_plan_tool_call",
]

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a simple decision engine.

Your task is to decide whether to call a function.

Available functions:
- start_plan: call this if the user wants to create or start a plan
- noop: call this if no action is needed

Rules:
- Call at most ONE function
- Do not explain your reasoning
- Do not return JSON
- Only call a function if appropriate
"""

_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "start_plan",
            "description": "Start or create a plan.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "noop",
            "description": "No action needed.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]


class PlanAgentEnvelopeError(ValueError):
    """Raised when the plan agent payload is invalid."""


def start_plan() -> Dict[str, str]:
    return {"user_text": "Starting a plan. Tell me what you'd like to plan."}


def noop() -> Dict[str, str]:
    return {"user_text": "No action needed."}


_PLAN_TOOL_HANDLERS = {
    "start_plan": start_plan,
    "noop": noop,
}


def run_plan_tool_call(tool_call: Dict[str, Any]) -> Dict[str, str]:
    name = tool_call.get("name") if isinstance(tool_call, dict) else None
    handler = _PLAN_TOOL_HANDLERS.get(name)
    if not handler:
        logger.warning("Unknown plan tool call: %s", name)
        return noop()
    return handler()


def _extract_tool_call(response: Any) -> Optional[Dict[str, Any]]:
    output = getattr(response, "output", None)
    if not output:
        return None
    tool_call_types = {"tool_call", "function_call"}
    for item in output:
        item_type = getattr(item, "type", None)
        if item_type is None and isinstance(item, dict):
            item_type = item.get("type")
        if item_type in tool_call_types:
            return {
                "name": getattr(item, "name", None) or (item.get("name") if isinstance(item, dict) else None),
                "id": getattr(item, "id", None) or (item.get("id") if isinstance(item, dict) else None),
                "arguments": getattr(item, "arguments", None)
                or (item.get("arguments") if isinstance(item, dict) else None),
            }

        content = getattr(item, "content", None)
        if content is None and isinstance(item, dict):
            content = item.get("content")
        if not content:
            continue
        for part in content:
            part_type = getattr(part, "type", None)
            if part_type is None and isinstance(part, dict):
                part_type = part.get("type")
            if part_type not in tool_call_types:
                continue
            return {
                "name": getattr(part, "name", None)
                or (part.get("name") if isinstance(part, dict) else None),
                "id": getattr(part, "id", None) or (part.get("id") if isinstance(part, dict) else None),
                "arguments": getattr(part, "arguments", None)
                or (part.get("arguments") if isinstance(part, dict) else None),
            }
    return None


def _build_messages(payload: Dict[str, Any]) -> list[dict[str, str]]:
    user_message = payload.get("message_text") if isinstance(payload, dict) else None
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_message or ""},
    ]


async def generate_plan_agent_response(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Send the Plan Agent payload to the LLM and return tool calls if present."""

    messages = _build_messages(payload)
    response = await async_client.responses.create(
        model=settings.PLAN_MODEL,
        input=messages,
        max_output_tokens=settings.MAX_TOKENS,
        tools=_TOOL_DEFINITIONS,
        tool_choice="auto",
    )

    tool_call = _extract_tool_call(response)
    reply_text = "" if tool_call else extract_output_text(response)

    return {
        "reply_text": reply_text,
        "tool_call": tool_call,
    }


async def plan_agent(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Wrapper used by the orchestrator to call the LLM-driven plan agent."""

    return await generate_plan_agent_response(payload)
