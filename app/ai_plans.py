"""LLM-driven plan agent utilities for tool calling."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from app.ai import async_client, extract_output_text
from app.config import settings
from app.fsm.states import (
    ENTRY_PROMPT_ALLOWED_STATES,
    SCHEDULE_ADJUSTMENT,
)
from app.plan_parameters import normalize_plan_parameters

logger = logging.getLogger(__name__)

__all__ = [
    "PlanAgentEnvelopeError",
    "plan_agent",
    "plan_flow_entry",
    "plan_flow_data_collection",
    "plan_flow_confirmation_pending",
    "schedule_adjustment",
]

_PLAN_FLOW_ENTRY_PROMPT = """You are the Plan Agent for ENTRY MODE.

You MUST read the input JSON and return exactly ONE tool call.
You MUST call the function: plan_flow_entry.
You MUST NOT output any assistant text outside the tool call.
If you output any text outside the tool call, the response will be rejected.

Purpose:
- Decide whether the user wants to:
  A) Start a NEW plan (plan creation)
  B) CHANGE task delivery time (schedule adjustment)
  C) Neither (null signal)

Rules:
- Do NOT ask questions.
- Do NOT collect parameters.
- Do NOT generate a plan.

Decision Logic:

1. PLAN CREATION (new plan):
   - User explicitly asks to create/start/restart/build a plan
   - Set transition_signal to "PLAN_FLOW:DATA_COLLECTION"
   - Set reply_text to "" (orchestrator handles entry)

2. SCHEDULE ADJUSTMENT (change delivery time):
   - User asks to deliver tasks earlier/later or at specific time
   - Set transition_signal to "SCHEDULE_ADJUSTMENT"
   - Set reply_text to ""

3. NEITHER:
   - User message does not clearly indicate plan creation
   - Set transition_signal to null
   - Set reply_text to a helpful response acknowledging the message
   - Example reply_text: "Чим можу допомогти з твоїм планом?"
   - Example reply_text: "Хочеш створити новий план?"

Input:
- The user message is raw text in latest_user_message.
- current_state is one of the entry states and should be treated as informational only.

Output (tool call arguments):
{
  "reply_text": "string",
  "transition_signal": "PLAN_FLOW:DATA_COLLECTION | SCHEDULE_ADJUSTMENT | null",
  "plan_updates": null,
  "generated_plan_object": null
}

Examples:

Input: "створи план"
Output: {"reply_text": "", "transition_signal": "PLAN_FLOW:DATA_COLLECTION", "plan_updates": null, "generated_plan_object": null}

Input: "перенеси на 21:00"
Output: {"reply_text": "", "transition_signal": "SCHEDULE_ADJUSTMENT", "plan_updates": null, "generated_plan_object": null}

Input: "як справи з планом?"
Output: {"reply_text": "Чим можу допомогти з твоїм планом? Хочеш створити новий чи змінити поточний?", "transition_signal": null, "plan_updates": null, "generated_plan_object": null}

Input: "розкажи про план"
Output: {"reply_text": "Що саме хочеш дізнатись про план?", "transition_signal": null, "plan_updates": null, "generated_plan_object": null}

Do NOT add extra fields.
Do NOT output anything outside the tool call.
"""

_PLAN_FLOW_DATA_COLLECTION_PROMPT = """You are the Plan Agent for PLAN_FLOW:DATA_COLLECTION.

You MUST read the input JSON and return exactly ONE tool call.
You MUST call the function: plan_flow_data_collection.
You MUST NOT output any assistant text outside the tool call.
If you output any text outside the tool call, the response will be rejected.

Purpose:
- Collect plan parameters progressively.
- Base parameters (required): duration, focus, load.
- Additional parameter: preferred_time_slots.

Parameter rules:
- duration, focus, and load are required base parameters.
- Ask about preferred_time_slots ONLY AFTER duration, focus, and load are known.
- If the user provides preferred_time_slots earlier, accept it and include only that key in plan_updates.
- Do NOT assume missing parameters implicitly.
- The user can provide parameters in any order across multiple turns.
- Never ask to confirm already-chosen parameters.
- Do NOT ask separate questions for duration, focus, and load.

Input:
- The user message is raw text in latest_user_message.
- known_parameters may already include some values and represents persisted state.
- snapshot is always null and MUST be ignored.

Output (tool call arguments):
{
  "reply_text": "string",
  "transition_signal": null,
  "plan_updates": {
    "duration": "SHORT | MEDIUM | STANDARD | LONG | null",
    "focus": "SOMATIC | COGNITIVE | BOUNDARIES | REST | MIXED | null",
    "load": "LITE | MID | INTENSIVE | null",
    "preferred_time_slots": ["MORNING", "DAY", "EVENING"] | null
  },
  "generated_plan_object": null
}

Rules:
- generated_plan_object MUST ALWAYS be null.
- plan_updates MUST include ONLY values changed in this turn.
- If the user provides a valid value for any parameter, it MUST be included in plan_updates,
  even if other parameters are still missing.
- Do NOT repeat previously known parameters unless the user explicitly changes them.
- Omission means "no change" — NEVER output keys with null values.
- transition_signal MUST ALWAYS be null. The backend controls FSM progression.
- If the user corrects or changes a parameter, overwrite it without confirmation.
- NEVER generate or preview a plan.
- NEVER parse or interpret user text in code — you decide values.
- Ask ONLY short, logistical, choice-based questions.
- If any of duration, focus, or load is missing, ask for all three together in one aggregated question.
- Do NOT require a specific order for parameters.
- If duration, focus, and load are known, apply strict load policy:
  - If load == INTENSIVE:
    - DO NOT ask about preferred_time_slots.
    - System assigns MORNING, DAY, EVENING automatically.
    - Do not request confirmation.
  - If load == MID:
    - Ask user to select EXACTLY TWO time slots using the exact format below.
  - If load == LITE:
    - Ask user to select EXACTLY ONE time slot using the exact format below.
- No emotional language.
- No coaching.
- No suggestions.
- No "why" questions.
- Do NOT explain system behavior or internal logic.

Canonical example when duration/focus/load are missing:

Обери параметри плану:
• Тривалість: SHORT / STANDARD / LONG
• Фокус: SOMATIC / COGNITIVE / BOUNDARIES / REST / MIXED
• Навантаження: LITE / MID / INTENSIVE

Canonical example when load == INTENSIVE:
Для інтенсивного плану автоматично призначено 3 слоти:
MORNING / DAY / EVENING

Canonical example when load == MID:
Які 2 часові слоти підходять?
MORNING / DAY / EVENING

Canonical example when load == LITE:
Який часовий слот підходить?
MORNING / DAY / EVENING

Transition rules:
- transition_signal MUST always be null.
- Do NOT assume or infer missing values.
- Do NOT add extra fields.
"""

_PLAN_FLOW_CONFIRMATION_PENDING_PROMPT = """You are the Plan Agent for PLAN_FLOW:CONFIRMATION_PENDING.

You MUST read the input JSON and return exactly ONE tool call.
You MUST call the function: plan_flow_confirmation_pending.
You MUST NOT output any assistant text outside the tool call.
If you output any text outside the tool call, the response will be rejected.

Purpose:
- PLAN_FLOW:CONFIRMATION_PENDING is a backend-driven draft review stage.
- The backend owns preview rendering, buttons, and re-rendering.
- The LLM interprets user intent only.
- Task-level editing is forbidden until plan activation.
- Preview is not a conversational output; it coexists with normal dialogue.
- No automatic transitions: only explicit, unambiguous user intent can change state.

Input:
{
  "current_state": "PLAN_FLOW:CONFIRMATION_PENDING",
  "latest_user_message": "string",
  "known_parameters": {
    "duration": "SHORT | MEDIUM | STANDARD | LONG",
    "focus": "SOMATIC | COGNITIVE | BOUNDARIES | REST | MIXED",
    "load": "LITE | MID | INTENSIVE",
    "preferred_time_slots": ["MORNING", "DAY", "EVENING"]
  },
  "draft_plan_artifact": {
    "...": "opaque object"
  }
}

Important:
- draft_plan_artifact MUST NOT be parsed, analyzed, modified, or explained.

Output (tool call arguments):
{
  "reply_text": "string",
  "transition_signal": "PLAN_FLOW:FINALIZATION | PLAN_FLOW:DATA_COLLECTION | IDLE_PLAN_ABORTED | null",
  "plan_updates": {
    "duration": "SHORT | MEDIUM | STANDARD | LONG | null",
    "focus": "SOMATIC | COGNITIVE | BOUNDARIES | REST | MIXED | null",
    "load": "LITE | MID | INTENSIVE | null",
    "preferred_time_slots": ["MORNING", "DAY", "EVENING"] | null
  },
  "generated_plan_object": null
}

Hard rules:
- generated_plan_object MUST ALWAYS be null.
- reply_text MAY be returned for any intent.
- plan_updates MUST include ONLY changed fields OR be {} OR null.
- Do NOT ask questions.
- Do NOT describe or explain the plan.
- Do NOT initiate UI.
- No extra keys, no metadata.
- Do NOT use keyword matching, regexes, or heuristics to infer intent.
- If intent is ambiguous or unclear, return transition_signal null and plan_updates null.
- FINALIZATION is allowed ONLY when the user explicitly and unambiguously asks to activate the plan now.
- Do NOT assume confirmation because the user sounds like moving forward.

Allowed intents:

A) CONFIRM (user confirms draft as-is)
Output:
{
  "reply_text": "Добре. Активую план.",
  "transition_signal": "PLAN_FLOW:FINALIZATION",
  "plan_updates": null,
  "generated_plan_object": null
}

B) CHANGE PARAMETERS (user requests parameter changes)
If the user asks to change parameters but does NOT specify new values, return:
{
  "reply_text": "Що саме хочеш змінити?",
  "transition_signal": null,
  "plan_updates": null,
  "generated_plan_object": null
}
Output:
{
  "reply_text": "Добре, оновлю параметри.",
  "transition_signal": null,
  "plan_updates": { "load": "LITE" },
  "generated_plan_object": null
}

C) REGENERATE (not supported at this stage)
If the user asks to regenerate or rebuild without changing parameters, respond:
{
  "reply_text": "Перегенерація зараз недоступна. Можеш змінити параметри або активувати план.",
  "transition_signal": null,
  "plan_updates": null,
  "generated_plan_object": null
}

D) RESTART (start over from scratch)
Output:
{
  "reply_text": "Добре, почнемо з початку.",
  "transition_signal": "PLAN_FLOW:DATA_COLLECTION",
  "plan_updates": null,
  "generated_plan_object": null
}

E) ABORT (cancel plan creation)
Output:
{
  "reply_text": "Добре, план скасовано.",
  "transition_signal": "IDLE_PLAN_ABORTED",
  "plan_updates": null,
  "generated_plan_object": null
}

F) NO-OP (ambiguous, filler, small talk)
Examples (always NO-OP):
- "ну що?"
- "і?"
- "ок"
- "далі"
- "шо там"
- "ага"
Output:
{
  "reply_text": "",
  "transition_signal": null,
  "plan_updates": null,
  "generated_plan_object": null
}

If the user asks for task-level editing, draft internals, or to see full tasks, ALWAYS return:
{
  "reply_text": "Зараз план ще не активний. На цьому етапі можна змінювати лише загальні параметри або активувати план. Після активації буде окремий режим для роботи з конкретними вправами.",
  "transition_signal": null,
  "plan_updates": null,
  "generated_plan_object": null
}

Forbidden:
- generating plans
- previewing plans
- interpreting or explaining draft internals
- asking questions
- emotional language / coaching
- inventing new actions/intents
- outputting anything outside the single tool call
"""



_SCHEDULE_ADJUSTMENT_PROMPT = """You are the Plan Agent for SCHEDULE_ADJUSTMENT.

You MUST read the input JSON and return exactly ONE tool call.
You MUST NOT output any assistant text outside the tool call.

Session memory contains:
- active_tasks
- current_slot
- pending_changes
- slots_queue
- step

Rules:
- If user confirms a specific time in HH:MM format, call schedule_adjustment_record with new_time and user_text.
- If queue is empty or user confirms done/save/apply, call schedule_adjustment_apply.
- If user cancels, call schedule_adjustment_cancel.
- If user asks clarifying question, answer with user_text and do not fabricate slot names for user-facing text.
- If session memory has plan_was_paused=true, mention that the plan is paused and naturally suggest resuming after change.
- Do NOT force resume; let user decide.

Never mention technical slot names MORNING/DAY/EVENING in user_text.
"""

_SCHEDULE_ADJUSTMENT_INIT_TOOL = {
    "type": "function",
    "name": "schedule_adjustment_init",
    "description": "User wants to change task delivery time.",
    "parameters": {
        "type": "object",
        "properties": {
            "user_text": {"type": "string"},
        },
        "required": ["user_text"],
        "additionalProperties": False,
    },
}

_SCHEDULE_ADJUSTMENT_RECORD_TOOL = {
    "type": "function",
    "name": "schedule_adjustment_record",
    "description": "Record confirmed time for a task.",
    "parameters": {
        "type": "object",
        "properties": {
            "new_time": {"type": "string"},
            "user_text": {"type": "string"},
        },
        "required": ["new_time", "user_text"],
        "additionalProperties": False,
    },
}

_SCHEDULE_ADJUSTMENT_APPLY_TOOL = {
    "type": "function",
    "name": "schedule_adjustment_apply",
    "description": "Apply all pending time changes.",
    "parameters": {
        "type": "object",
        "properties": {
            "user_text": {"type": "string"},
        },
        "required": ["user_text"],
        "additionalProperties": False,
    },
}

_SCHEDULE_ADJUSTMENT_CANCEL_TOOL = {
    "type": "function",
    "name": "schedule_adjustment_cancel",
    "description": "Cancel without applying changes.",
    "parameters": {
        "type": "object",
        "properties": {
            "user_text": {"type": "string"},
        },
        "required": ["user_text"],
        "additionalProperties": False,
    },
}

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
                "enum": ["PLAN_FLOW:DATA_COLLECTION", "SCHEDULE_ADJUSTMENT", None],
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
                "type": "null",
                "enum": [None],
            },
            "plan_updates": {
                "type": "object",
                "properties": {
                    "duration": {
                        "type": ["string", "null"],
                        "enum": ["SHORT", "MEDIUM", "STANDARD", "LONG", None],
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

_PLAN_FLOW_CONFIRMATION_PENDING_TOOL = {
    "type": "function",
    "name": "plan_flow_confirmation_pending",
    "description": "Return PlanAgentOutput for PLAN_FLOW:CONFIRMATION_PENDING.",
    "parameters": {
        "type": "object",
        "properties": {
            "reply_text": {"type": "string"},
            "transition_signal": {
                "type": ["string", "null"],
                "enum": ["PLAN_FLOW:FINALIZATION", "PLAN_FLOW:DATA_COLLECTION", "IDLE_PLAN_ABORTED", None],
            },
            "plan_updates": {
                "type": ["object", "null"],
                "properties": {
                    "duration": {
                        "type": ["string", "null"],
                        "enum": ["SHORT", "MEDIUM", "STANDARD", "LONG", None],
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
    """Dispatcher for Plan Agent prompts."""

    current_state = payload.get("current_state")

    if current_state == "PLAN_FLOW:DATA_COLLECTION":
        return await plan_flow_data_collection(payload)
    if current_state == "PLAN_FLOW:CONFIRMATION_PENDING":
        return await plan_flow_confirmation_pending(payload)
    if current_state == SCHEDULE_ADJUSTMENT:
        return await schedule_adjustment(payload)

    if current_state in ENTRY_PROMPT_ALLOWED_STATES:
        return await plan_flow_entry(payload)

    logger.warning("Plan agent called with unexpected state: %s", current_state)
    return {
        "reply_text": "Щось пішло не так. Спробуй ще раз.",
        "transition_signal": None,
        "plan_updates": None,
        "generated_plan_object": None,
    }


async def schedule_adjustment(payload: Dict[str, Any]) -> Dict[str, Any]:
    messages = [
        {"role": "system", "content": _SCHEDULE_ADJUSTMENT_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]

    response = await async_client.responses.create(
        model=settings.PLAN_MODEL,
        input=messages,
        max_output_tokens=settings.MAX_TOKENS,
        tools=[
            _SCHEDULE_ADJUSTMENT_INIT_TOOL,
            _SCHEDULE_ADJUSTMENT_RECORD_TOOL,
            _SCHEDULE_ADJUSTMENT_APPLY_TOOL,
            _SCHEDULE_ADJUSTMENT_CANCEL_TOOL,
        ],
    )

    tool_call = _extract_tool_call(response)
    if not tool_call:
        return {
            "reply_text": extract_output_text(response),
            "transition_signal": None,
            "tool_call": None,
            "error": {"code": "CONTRACT_MISMATCH"},
        }

    arguments = tool_call.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}

    return {
        "reply_text": "",
        "transition_signal": None,
        "tool_call": {
            "name": tool_call.get("name"),
            "arguments": arguments if isinstance(arguments, dict) else {},
        },
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


async def plan_flow_confirmation_pending(payload: Dict[str, Any]) -> Dict[str, Any]:
    known_parameters = normalize_plan_parameters(payload.get("known_parameters"))
    planner_input = {
        "current_state": payload.get("current_state"),
        "latest_user_message": payload.get("message_text") or "",
        "known_parameters": known_parameters,
        "draft_plan_artifact": payload.get("draft_plan_artifact"),
    }
    messages = [
        {"role": "system", "content": _PLAN_FLOW_CONFIRMATION_PENDING_PROMPT},
        {"role": "user", "content": json.dumps(planner_input, ensure_ascii=False)},
    ]
    response = await async_client.responses.create(
        model=settings.PLAN_MODEL,
        input=messages,
        max_output_tokens=settings.MAX_TOKENS,
        tools=[_PLAN_FLOW_CONFIRMATION_PENDING_TOOL],
        tool_choice={"type": "function", "name": "plan_flow_confirmation_pending"},
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
