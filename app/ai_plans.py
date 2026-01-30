"""LLM-driven plan agent utilities for tool calling."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from app.ai import async_client, extract_output_text
from app.config import settings
from app.plan_parameters import normalize_plan_parameters

__all__ = [
    "PlanAgentEnvelopeError",
    "plan_agent",
    "plan_flow_entry",
    "plan_flow_data_collection",
]

_PLAN_FLOW_ENTRY_PROMPT = """You are the Plan Agent for ENTRY MODE.

You MUST read the input JSON and return exactly ONE tool call.
You MUST call the function: plan_flow_entry.
You MUST NOT output any assistant text outside the tool call.
If you output any text outside the tool call, the response will be rejected.

Purpose:
- Decide whether the user explicitly wants to start a NEW plan.

Rules:
- Do NOT ask questions.
- Do NOT collect parameters.
- Do NOT generate a plan.
- Do NOT respond to the user in text.
- reply_text MUST be an empty string.

Decision:
- If the user explicitly asks to create/start/restart a plan, set transition_signal to
  PLAN_FLOW:DATA_COLLECTION.
- Otherwise, transition_signal MUST be null.

Input:
- The user message is raw text in latest_user_message.
- current_state is one of the entry states and should be treated as informational only.

Output (tool call arguments):
{
  "reply_text": "",
  "transition_signal": "PLAN_FLOW:DATA_COLLECTION | null",
  "plan_updates": null,
  "generated_plan_object": null
}

Do NOT add extra fields.
"""

_PLAN_FLOW_DATA_COLLECTION_PROMPT = """You are the Plan Agent for PLAN_FLOW:DATA_COLLECTION.

You MUST read the input JSON and return exactly ONE tool call.
You MUST call the function: plan_flow_data_collection.
You MUST NOT output any assistant text outside the tool call.
If you output any text outside the tool call, the response will be rejected.

Purpose:
- Collect plan parameters progressively.
- Base parameters (required): duration, focus, load.
- Dependent parameter: preferred_time_slots.

Parameter rules:
- duration, focus, and load are required base parameters.
- preferred_time_slots MUST be collected ONLY AFTER load is defined.
- Do NOT ask about preferred_time_slots if load is null.
- preferred_time_slots MAY be set in the same turn only if load is set in plan_updates.
- Do NOT assume missing parameters implicitly.

Input:
- The user message is raw text in latest_user_message.
- known_parameters may already include some values.
- snapshot is always null and MUST be ignored.

Output (tool call arguments):
{
  "reply_text": "string",
  "transition_signal": "PLAN_FLOW:CONFIRMATION_PENDING | null",
  "plan_updates": {
    "duration": "SHORT | STANDARD | LONG | null",
    "focus": "SOMATIC | COGNITIVE | BOUNDARIES | REST | MIXED | null",
    "load": "LITE | MID | INTENSIVE | null",
    "preferred_time_slots": ["MORNING", "DAY", "EVENING"] | null
  },
  "generated_plan_object": null
}

Rules:
- generated_plan_object MUST ALWAYS be null.
- plan_updates MUST include ONLY values changed in this turn.
- If the user corrects or changes a parameter, overwrite it without confirmation.
- NEVER generate or preview a plan.
- NEVER parse or interpret user text in code — you decide values.
- Ask ONLY short, logistical, choice-based questions.
- Ask about ALL missing base parameters in one message when possible.
- No emotional language.
- No coaching.
- No suggestions.
- No "why" questions.
- Do NOT explain system behavior or internal logic.

Transition rules:
- If duration, focus, load, and preferred_time_slots are ALL defined explicitly (non-null)
  in known_parameters ∪ plan_updates after updates,
  set transition_signal to PLAN_FLOW:CONFIRMATION_PENDING.
- Otherwise, transition_signal MUST be null.
- No other transitions are allowed.
- Do NOT assume or infer missing values.
- Do NOT add extra fields.
"""

_PLAN_FLOW_ENTRY_TOOL = {
    "type": "function",
    "name": "plan_flow_entry",
    "description": "Return PlanAgentOutput for ENTRY MODE.",
    "parameters": {
        "type": "object",
        "properties": {
            "reply_text": {"type": "string"},
            "transition_signal": {
                "type": ["string", "null"],
                "enum": ["PLAN_FLOW:DATA_COLLECTION", None],
            },
            "plan_updates": {"type": "null"},
            "generated_plan_object": {"type": "null"},
        },
        "required": ["reply_text", "transition_signal", "plan_updates", "generated_plan_object"],
        "additionalProperties": False,
    },
}

_PLAN_FLOW_DATA_COLLECTION_TOOL = {
    "type": "function",
    "name": "plan_flow_data_collection",
    "description": "Return PlanAgentOutput for PLAN_FLOW:DATA_COLLECTION.",
    "parameters": {
        "type": "object",
        "properties": {
            "reply_text": {"type": "string"},
            "transition_signal": {
                "type": ["string", "null"],
                "enum": ["PLAN_FLOW:CONFIRMATION_PENDING", None],
            },
            "plan_updates": {
                "type": "object",
                "properties": {
                    "duration": {
                        "type": ["string", "null"],
                        "enum": ["SHORT", "STANDARD", "LONG", None],
                    },
                    "focus": {
                        "type": ["string", "null"],
                        "enum": ["SOMATIC", "COGNITIVE", "BOUNDARIES", "REST", "MIXED", None],
                    },
                    "load": {
                        "type": ["string", "null"],
                        "enum": ["LITE", "MID", "INTENSIVE", None],
                    },
                    "preferred_time_slots": {
                        "type": ["array", "null"],
                        "items": {"type": "string", "enum": ["MORNING", "DAY", "EVENING"]},
                    },
                },
                "additionalProperties": False,
            },
            "generated_plan_object": {"type": "null"},
        },
        "required": ["reply_text", "transition_signal", "plan_updates", "generated_plan_object"],
        "additionalProperties": False,
    },
}


class PlanAgentEnvelopeError(ValueError):
    """Raised when the plan agent payload is invalid."""


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


async def plan_agent(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Wrapper used by the orchestrator to call the LLM-driven plan agent."""

    current_state = payload.get("current_state")
    entry_states = {
        "IDLE_ONBOARDED",
        "IDLE_FINISHED",
        "IDLE_DROPPED",
        "IDLE_PLAN_ABORTED",
        "ACTIVE",
    }
    if current_state in entry_states:
        return await plan_flow_entry(payload)
    if current_state == "PLAN_FLOW:DATA_COLLECTION":
        return await plan_flow_data_collection(payload)
    return {
        "reply_text": "",
        "transition_signal": None,
        "plan_updates": None,
        "generated_plan_object": None,
    }


async def plan_flow_data_collection(payload: Dict[str, Any]) -> Dict[str, Any]:
    known_parameters = normalize_plan_parameters(payload.get("known_parameters"))
    planner_input = {
        "current_state": payload.get("current_state"),
        "known_parameters": known_parameters,
        "latest_user_message": payload.get("message_text") or "",
        "user_policy": payload.get("user_policy") or {},
        "snapshot": None,
    }
    messages = [
        {"role": "system", "content": _PLAN_FLOW_DATA_COLLECTION_PROMPT},
        {"role": "user", "content": json.dumps(planner_input, ensure_ascii=False)},
    ]
    response = await async_client.responses.create(
        model=settings.PLAN_MODEL,
        input=messages,
        max_output_tokens=settings.MAX_TOKENS,
        tools=[_PLAN_FLOW_DATA_COLLECTION_TOOL],
        tool_choice={"type": "function", "name": "plan_flow_data_collection"},
    )
    tool_call = _extract_tool_call(response)
    if not tool_call:
        return {
            "reply_text": extract_output_text(response),
            "transition_signal": None,
            "plan_updates": None,
            "generated_plan_object": None,
            "error": {"code": "CONTRACT_MISMATCH"},
        }
    arguments = tool_call.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return {
                "reply_text": extract_output_text(response),
                "transition_signal": None,
                "plan_updates": None,
                "generated_plan_object": None,
                "error": {"code": "CONTRACT_MISMATCH"},
            }
    if not isinstance(arguments, dict):
        return {
            "reply_text": extract_output_text(response),
            "transition_signal": None,
            "plan_updates": None,
            "generated_plan_object": None,
            "error": {"code": "CONTRACT_MISMATCH"},
        }
    return arguments


async def plan_flow_entry(payload: Dict[str, Any]) -> Dict[str, Any]:
    planner_input = {
        "current_state": payload.get("current_state"),
        "latest_user_message": payload.get("message_text") or "",
        "snapshot": None,
    }
    messages = [
        {"role": "system", "content": _PLAN_FLOW_ENTRY_PROMPT},
        {"role": "user", "content": json.dumps(planner_input, ensure_ascii=False)},
    ]
    response = await async_client.responses.create(
        model=settings.PLAN_MODEL,
        input=messages,
        max_output_tokens=settings.MAX_TOKENS,
        tools=[_PLAN_FLOW_ENTRY_TOOL],
        tool_choice={"type": "function", "name": "plan_flow_entry"},
    )
    tool_call = _extract_tool_call(response)
    if not tool_call:
        return {
            "reply_text": "",
            "transition_signal": None,
            "plan_updates": None,
            "generated_plan_object": None,
            "error": {"code": "CONTRACT_MISMATCH"},
        }
    arguments = tool_call.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return {
                "reply_text": "",
                "transition_signal": None,
                "plan_updates": None,
                "generated_plan_object": None,
                "error": {"code": "CONTRACT_MISMATCH"},
            }
    if not isinstance(arguments, dict):
        return {
            "reply_text": "",
            "transition_signal": None,
            "plan_updates": None,
            "generated_plan_object": None,
            "error": {"code": "CONTRACT_MISMATCH"},
        }
    return arguments
