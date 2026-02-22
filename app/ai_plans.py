"""LLM-driven plan agent utilities for tool calling."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from app.ai import async_client, extract_output_text
from app.config import settings
from app.fsm.states import (
    ADAPTATION_CONFIRMATION,
    ADAPTATION_PARAMS,
    ADAPTATION_SELECTION,
    ENTRY_PROMPT_ALLOWED_STATES,
)
from app.plan_parameters import normalize_plan_parameters
from app.adaptation_types import get_all_intent_values, get_intents_requiring_params

logger = logging.getLogger(__name__)

__all__ = [
    "PlanAgentEnvelopeError",
    "plan_agent",
    "plan_flow_entry",
    "plan_flow_data_collection",
    "plan_flow_confirmation_pending",
    "adaptation_flow_selection",
    "adaptation_flow_params",
    "adaptation_flow_confirmation",
]

_PLAN_FLOW_ENTRY_PROMPT = """You are the Plan Agent for ENTRY MODE.

You MUST read the input JSON and return exactly ONE tool call.
You MUST call the function: plan_flow_entry.
You MUST NOT output any assistant text outside the tool call.
If you output any text outside the tool call, the response will be rejected.

Purpose:
- Decide whether the user wants to:
  A) Start a NEW plan (plan creation)
  B) MODIFY existing plan (plan adaptation)
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

2. PLAN ADAPTATION (modify existing):
   - User wants to change/modify/adjust existing plan
   - Set transition_signal to "ADAPTATION_SELECTION"
   - Set reply_text to "" (orchestrator handles entry)

3. NEITHER:
   - User message does not clearly indicate plan creation or adaptation
   - Set transition_signal to null
   - Set reply_text to a helpful response acknowledging the message
   - Example reply_text: "Чим можу допомогти з твоїм планом?"
   - Example reply_text: "Хочеш створити новий план чи змінити поточний?"

Input:
- The user message is raw text in latest_user_message.
- current_state is one of the entry states and should be treated as informational only.

Output (tool call arguments):
{
  "reply_text": "string",
  "transition_signal": "PLAN_FLOW:DATA_COLLECTION | ADAPTATION_SELECTION | null",
  "plan_updates": null,
  "generated_plan_object": null
}

Examples:

Input: "створи план"
Output: {"reply_text": "", "transition_signal": "PLAN_FLOW:DATA_COLLECTION", "plan_updates": null, "generated_plan_object": null}

Input: "хочу змінити план"
Output: {"reply_text": "", "transition_signal": "ADAPTATION_SELECTION", "plan_updates": null, "generated_plan_object": null}

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
    "duration": "SHORT | STANDARD | LONG",
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
    "duration": "SHORT | STANDARD | LONG | null",
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

_ADAPTATION_FLOW_SELECTION_PROMPT = """You are the Plan Agent for ADAPTATION_SELECTION.

You MUST read the input JSON and return exactly ONE tool call.
You MUST call the function: adaptation_flow_selection.
You MUST NOT output any assistant text outside the tool call.

PURPOSE:
User wants to adapt their active plan but hasn't chosen which adaptation yet.
Your ONLY job: present available options clearly.

INPUT:
{
  "current_state": "ADAPTATION_SELECTION",
  "message_text": "string",
  "available_adaptations": [
    "<ADAPTATION_INTENT>",
    ...
  ],
  "active_plan": {
    "load": "LITE | MID | INTENSIVE",
    "duration": 7 | 14 | 21 | 90,
    "status": "active | paused",
    "preferred_time_slots": ["MORNING", "DAY", "EVENING"]
  }
}

OUTPUT:
{
  "reply_text": "string",
  "transition_signal": "ADAPTATION_PARAMS | ADAPTATION_CONFIRMATION | ACTIVE | null",
  "adaptation_intent": "<ADAPTATION_INTENT> | null",
  "adaptation_params": null
}

BEHAVIOR:

If message_text is UNCLEAR or user just said "хочу змінити план":
- List ALL adaptations from available_adaptations array
- Use SHORT descriptions (1 line each)
- Format as bullet list
- transition_signal = null
- adaptation_intent = null

If message_text contains CLEAR intent:
- Match to one of available_adaptations
- If matched adaptation needs params → transition to PARAMS
- If matched adaptation needs NO params → transition to CONFIRMATION
- Set adaptation_intent

If user says ABORT:
{
  "reply_text": "Добре, скасовано.",
  "transition_signal": "ACTIVE",
  "adaptation_intent": null,
  "adaptation_params": null
}

ADAPTATIONS REQUIRING PARAMS:
- CHANGE_MAIN_CATEGORY (needs target_category)
- EXTEND_PLAN_DURATION (needs target_duration)
- SHORTEN_PLAN_DURATION (needs target_duration)
- REDUCE_DAILY_LOAD (needs slot_to_remove)
- INCREASE_DAILY_LOAD (needs slot_to_add) — EXCEPT when load is already MID (2 slots → auto-assigned)

ALL OTHER ADAPTATIONS: no params needed

HARD RULES:
1. ONLY list adaptations from available_adaptations array
2. NEVER invent new adaptation types
3. NEVER explain psychology or motivation
4. NEVER recommend specific adaptations
5. Keep descriptions SHORT (max 5 words)
"""

_ADAPTATION_FLOW_PARAMS_PROMPT = """You are the Plan Agent for ADAPTATION_PARAMS.

You MUST read the input JSON and return exactly ONE tool call.
You MUST call the function: adaptation_flow_params.
You MUST NOT output any assistant text outside the tool call.

PURPOSE:
Adaptation type is KNOWN but parameters are MISSING.
Your ONLY job: collect the missing parameter.

INPUT:
{
  "current_state": "ADAPTATION_PARAMS",
  "message_text": "string",
  "adaptation_context": {
    "intent": "CHANGE_MAIN_CATEGORY | EXTEND_PLAN_DURATION | SHORTEN_PLAN_DURATION",
    "params": {
      "target_category": null | "somatic" | ...,
      "target_duration": null | 7 | 14 | 21 | 90
    }
  },
  "active_plan": {
    "duration": 7 | 14 | 21 | 90,
    "load": "LITE | MID | INTENSIVE",
    "preferred_time_slots": ["MORNING", "DAY", "EVENING"],
    "current_day": 1..N
  }
}

OUTPUT:
{
  "reply_text": "string",
  "transition_signal": "ADAPTATION_CONFIRMATION | ACTIVE | null",
  "adaptation_intent": "[SAME AS INPUT]",
  "adaptation_params": {
    "target_category": "somatic | cognitive | boundaries | rest | mixed | null",
    "target_duration": 7 | 14 | 21 | 90 | null,
    "slot_to_remove": "MORNING | DAY | EVENING | null",
    "slot_to_add": "MORNING | DAY | EVENING | null"
  }
}

BEHAVIOR:

If param is MISSING (null in input):
- Ask for that specific param
- Show allowed values ONLY
- Keep question SHORT
- transition_signal = null

If param is PROVIDED (present in message_text):
- Extract param value
- Set adaptation_params with extracted value
- transition_signal = "ADAPTATION_CONFIRMATION"

If user says ABORT:
{
  "reply_text": "Добре, скасовано.",
  "transition_signal": "ACTIVE",
  "adaptation_intent": null,
  "adaptation_params": null
}

PARAMETER RULES:

CHANGE_MAIN_CATEGORY:
- Param: target_category
- Allowed: somatic, cognitive, boundaries, rest, mixed
- Question: "Обери нову категорію: somatic / cognitive / boundaries / rest / mixed"

EXTEND_PLAN_DURATION:
- Param: target_duration
- Compute allowed targets from active_plan.duration:
  - current = 7  → allowed: [14, 21, 90]
  - current = 14 → allowed: [21, 90]
  - current = 21 → allowed: [90]
  - current = 90 → no extension available → reply: "План вже максимальної тривалості." → transition_signal = "ACTIVE"
- If user requests a target not in allowed list:
  - reply: "Для поточної тривалості ({duration}) доступно: {allowed_targets}."
  - transition_signal = null (re-ask)
- Question MUST include context (always show all three pieces):
  1. Current position: "Ти на дні {current_day} з {duration}."
  2. What changes: "Додасться {target - duration} нових днів."
  3. Confirmation: "Продовжити до {target} днів?"
- If only one allowed target → go directly to ADAPTATION_CONFIRMATION,
  do NOT ask user to choose — just show the context message above

SHORTEN_PLAN_DURATION:
- Param: target_duration
- Compute allowed targets from active_plan.duration AND active_plan.current_day:
  - current = 90 → candidates: [21]
  - current = 21 → candidates: [7, 14]
  - Filter rule: REMOVE any candidate where candidate <= active_plan.current_day
    (cannot shorten to a day already passed)
  - Final allowed = candidates after filter
- If final allowed is EMPTY:
  reply: "Скорочення недоступне — ти вже пройшов усі можливі точки скорочення."
  transition_signal = "ACTIVE"
- If user requests a specific target that is NOT in final allowed:
  reply: "День {requested_target} вже пройдено (ти на дні {current_day}).
          Доступні варіанти скорочення: {final_allowed}."
  transition_signal = null (re-ask)
- Question MUST include context:
  1. Current position: "Ти на дні {current_day} з {duration}."
  2. What changes: "Після скорочення залишиться {target - current_day} днів."
  3. Options: "Скоротити до: {final_allowed}."

REDUCE_DAILY_LOAD:
- Param: slot_to_remove
- Allowed values: ONLY slots currently in active_plan.preferred_time_slots
- Question: "Який часовий слот прибрати? Доступно: [list active slots]"
- NEVER show slots not in preferred_time_slots

INCREASE_DAILY_LOAD:
- Param: slot_to_add
- Case 1: load == "LITE" (1 slot active):
  - Allowed: slots NOT in preferred_time_slots (from MORNING, DAY, EVENING)
  - Question: "Який часовий слот додати? Доступно: [list available slots]"
- Case 2: load == "MID" (2 slots active):
  - Only 1 slot remains → set slot_to_add automatically to missing slot
  - transition_signal = "ADAPTATION_CONFIRMATION" immediately
  - NO question to user
- NEVER show slots already in preferred_time_slots

HARD RULES:
1. adaptation_intent MUST NOT change (pass through from input)
2. ONLY collect ONE parameter
3. NEVER recommend which value to choose
4. NEVER explain why parameter is needed
5. Show ONLY allowed values
6. You MAY list allowed values from schema.
"""

_ADAPTATION_FLOW_CONFIRMATION_PROMPT = """You are the Plan Agent for ADAPTATION_CONFIRMATION.

You MUST read the input JSON and return exactly ONE tool call.
You MUST call the function: adaptation_flow_confirmation.
You MUST NOT output any assistant text outside the tool call.

PURPOSE:
Adaptation type and all params are KNOWN and LOCKED.
Your ONLY job: show preview and get confirmation.

INPUT:
{
  "current_state": "ADAPTATION_CONFIRMATION",
  "message_text": "string",
  "adaptation_context": {
    "intent": "<ADAPTATION_INTENT>",
    "params": {...}
  },
  "active_plan": {
    "load": "LITE | MID | INTENSIVE",
    "duration": 7 | 14 | 21 | 90,
    "focus": "SOMATIC | ...",
    "daily_task_count": 1 | 2 | 3,
    "difficulty_level": 1 | 2 | 3
  }
}

OUTPUT:
{
  "reply_text": "string",
  "transition_signal": "EXECUTE_ADAPTATION | ADAPTATION_PARAMS | ACTIVE | null",
  "adaptation_intent": "[SAME AS INPUT - FROZEN]",
  "adaptation_params": "[SAME AS INPUT - FROZEN]",
  "confirmed": true | false
}

CRITICAL INVARIANT:
adaptation_intent and adaptation_params are FROZEN in this state.
They MUST be copied verbatim from input.
They CANNOT be changed under ANY circumstances.

BEHAVIOR:

If FIRST TIME in confirmation (no user response yet):
- Show preview of changes
- Use ONLY values from active_plan (don't invent numbers)
- Ask "Підтвердити?"
- transition_signal = null
- confirmed = false

If user says YES/CONFIRM:
{
  "reply_text": "Застосовую зміни.",
  "transition_signal": "EXECUTE_ADAPTATION",
  "adaptation_intent": "[FROZEN]",
  "adaptation_params": "[FROZEN]",
  "confirmed": true
}

If user wants to EDIT param:
{
  "reply_text": "Обери нову категорію:",
  "transition_signal": "ADAPTATION_PARAMS",
  "adaptation_intent": "[FROZEN]",
  "adaptation_params": "[FROZEN - but with param set to null]",
  "confirmed": false
}

If user wants DIFFERENT adaptation:
{
  "reply_text": "Для іншої зміни скасуй поточну. Скасувати?",
  "transition_signal": null,
  "adaptation_intent": "[FROZEN]",
  "adaptation_params": "[FROZEN]",
  "confirmed": false
}

If user wants DIFFERENT adaptation AND confirms abort:
{
  "reply_text": "Добре, скасовано.",
  "transition_signal": "ACTIVE",
  "adaptation_intent": null,
  "adaptation_params": null,
  "confirmed": false
}

If user says ABORT:
{
  "reply_text": "Добре, скасовано.",
  "transition_signal": "ACTIVE",
  "adaptation_intent": null,
  "adaptation_params": null,
  "confirmed": false
}

PREVIEW FORMAT RULES:

Use ONLY values from active_plan payload:
- daily_task_count (not load label)
- difficulty_level (not qualitative descriptions)
- duration (number of days)

HARD RULES:
1. NEVER change adaptation_intent (copy from input)
2. NEVER change adaptation_params (copy from input)
3. NEVER invent numbers not in active_plan
4. NEVER explain WHY changes are good
5. Show ONLY structural facts
"""

_ADAPTATION_FLOW_SELECTION_TOOL = {
    "type": "function",
    "name": "adaptation_flow_selection",
    "description": "Handle SELECTION state of adaptation flow",
    "parameters": {
        "type": "object",
        "properties": {
            "reply_text": {"type": "string"},
            "transition_signal": {
                "type": ["string", "null"],
                "enum": [
                    "ADAPTATION_PARAMS",
                    "ADAPTATION_CONFIRMATION",
                    "ACTIVE",
                    None,
                ],
            },
            "adaptation_intent": {
                "type": ["string", "null"],
                "enum": get_all_intent_values() + [None],
            },
            "adaptation_params": {"type": "null"},
        },
        "required": ["reply_text", "transition_signal", "adaptation_intent", "adaptation_params"],
        "additionalProperties": False,
    },
}

_ADAPTATION_FLOW_PARAMS_TOOL = {
    "type": "function",
    "name": "adaptation_flow_params",
    "description": "Handle PARAMS state of adaptation flow",
    "parameters": {
        "type": "object",
        "properties": {
            "reply_text": {"type": "string"},
            "transition_signal": {
                "type": ["string", "null"],
                "enum": ["ADAPTATION_CONFIRMATION", "ACTIVE", None],
            },
            "adaptation_intent": {
                "type": "string",
                "enum": get_intents_requiring_params(),
            },
            "adaptation_params": {
                "type": ["object", "null"],
                "properties": {
                    "target_category": {
                        "type": ["string", "null"],
                        "enum": ["somatic", "cognitive", "boundaries", "rest", "mixed", None],
                    },
                    "target_duration": {
                        "type": ["integer", "null"],
                        "enum": [7, 14, 21, 90, None],
                    },
                },
                "additionalProperties": False,
            },
        },
        "required": ["reply_text", "transition_signal", "adaptation_intent", "adaptation_params"],
        "additionalProperties": False,
    },
}

_ADAPTATION_FLOW_CONFIRMATION_TOOL = {
    "type": "function",
    "name": "adaptation_flow_confirmation",
    "description": "Handle CONFIRMATION state of adaptation flow",
    "parameters": {
        "type": "object",
        "properties": {
            "reply_text": {"type": "string"},
            "transition_signal": {
                "type": ["string", "null"],
                "enum": ["EXECUTE_ADAPTATION", "ADAPTATION_PARAMS", "ACTIVE", None],
            },
            "adaptation_intent": {
                "type": "string",
                "enum": get_all_intent_values(),
            },
            "adaptation_params": {"type": ["object", "null"]},
            "confirmed": {"type": "boolean"},
        },
        "required": [
            "reply_text",
            "transition_signal",
            "adaptation_intent",
            "adaptation_params",
            "confirmed",
        ],
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
                "enum": ["PLAN_FLOW:DATA_COLLECTION", "ADAPTATION_SELECTION", None],
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
    """Dispatcher for Plan Agent prompts."""

    current_state = payload.get("current_state")

    if current_state == "PLAN_FLOW:DATA_COLLECTION":
        return await plan_flow_data_collection(payload)
    if current_state == "PLAN_FLOW:CONFIRMATION_PENDING":
        return await plan_flow_confirmation_pending(payload)
    if current_state == ADAPTATION_SELECTION:
        return await adaptation_flow_selection(payload)
    if current_state == ADAPTATION_PARAMS:
        return await adaptation_flow_params(payload)
    if current_state == ADAPTATION_CONFIRMATION:
        return await adaptation_flow_confirmation(payload)

    if current_state in ENTRY_PROMPT_ALLOWED_STATES:
        return await plan_flow_entry(payload)

    logger.warning("Plan agent called with unexpected state: %s", current_state)
    return {
        "reply_text": "Щось пішло не так. Спробуй ще раз.",
        "transition_signal": None,
        "plan_updates": None,
        "generated_plan_object": None,
    }


async def adaptation_flow_selection(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Handle SELECTION state.

    Returns adaptation_intent as AdaptationIntent.value or None.
    """
    messages = [
        {"role": "system", "content": _ADAPTATION_FLOW_SELECTION_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]

    response = await async_client.responses.create(
        model=settings.PLAN_MODEL,
        input=messages,
        max_output_tokens=settings.MAX_TOKENS,
        tools=[_ADAPTATION_FLOW_SELECTION_TOOL],
        tool_choice={"type": "function", "name": "adaptation_flow_selection"},
    )

    tool_call = _extract_tool_call(response)
    if not tool_call:
        return {
            "reply_text": extract_output_text(response),
            "transition_signal": None,
            "adaptation_intent": None,
            "adaptation_params": None,
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
                "adaptation_intent": None,
                "adaptation_params": None,
                "error": {"code": "CONTRACT_MISMATCH"},
            }

    if not isinstance(arguments, dict):
        return {
            "reply_text": "",
            "transition_signal": None,
            "adaptation_intent": None,
            "adaptation_params": None,
            "error": {"code": "CONTRACT_MISMATCH"},
        }

    return arguments


async def adaptation_flow_params(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Handle PARAMS state.

    Returns adaptation_intent as required AdaptationIntent.value.
    """
    messages = [
        {"role": "system", "content": _ADAPTATION_FLOW_PARAMS_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]

    response = await async_client.responses.create(
        model=settings.PLAN_MODEL,
        input=messages,
        max_output_tokens=settings.MAX_TOKENS,
        tools=[_ADAPTATION_FLOW_PARAMS_TOOL],
        tool_choice={"type": "function", "name": "adaptation_flow_params"},
    )

    tool_call = _extract_tool_call(response)
    if not tool_call:
        return {
            "reply_text": extract_output_text(response),
            "transition_signal": None,
            "adaptation_intent": None,
            "adaptation_params": None,
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
                "adaptation_intent": None,
                "adaptation_params": None,
                "error": {"code": "CONTRACT_MISMATCH"},
            }

    if not isinstance(arguments, dict):
        return {
            "reply_text": "",
            "transition_signal": None,
            "adaptation_intent": None,
            "adaptation_params": None,
            "error": {"code": "CONTRACT_MISMATCH"},
        }

    return arguments


async def adaptation_flow_confirmation(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Handle CONFIRMATION state.

    Returns adaptation_intent frozen from input as AdaptationIntent.value.
    """
    messages = [
        {"role": "system", "content": _ADAPTATION_FLOW_CONFIRMATION_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]

    response = await async_client.responses.create(
        model=settings.PLAN_MODEL,
        input=messages,
        max_output_tokens=settings.MAX_TOKENS,
        tools=[_ADAPTATION_FLOW_CONFIRMATION_TOOL],
        tool_choice={"type": "function", "name": "adaptation_flow_confirmation"},
    )

    tool_call = _extract_tool_call(response)
    if not tool_call:
        return {
            "reply_text": extract_output_text(response),
            "transition_signal": None,
            "adaptation_intent": None,
            "adaptation_params": None,
            "confirmed": False,
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
                "adaptation_intent": None,
                "adaptation_params": None,
                "confirmed": False,
                "error": {"code": "CONTRACT_MISMATCH"},
            }

    if not isinstance(arguments, dict):
        return {
            "reply_text": "",
            "transition_signal": None,
            "adaptation_intent": None,
            "adaptation_params": None,
            "confirmed": False,
            "error": {"code": "CONTRACT_MISMATCH"},
        }

    return arguments


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
