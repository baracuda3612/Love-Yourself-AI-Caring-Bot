import json
import re
import time
from typing import Any, Dict

from app.ai import async_client, extract_output_text
from app.config import settings
from app.logging.router_logging import log_router_decision

ROUTER_SYSTEM_PROMPT = (
    "You are an intent router for a mental wellbeing bot in Telegram. "
    "Classify the latest user message using the dialogue history and current bot state. "
    "You must always consider: current_state, last_bot_message, recent_messages, message_text, user_profile. "
    "Intents: \"manager_flow\" (settings, buttons, commands, short factual answers to bot prompts), "
    "\"coach_dialog\" (empathetic conversation and emotional sharing), "
    "\"onboarding_interruption\" (user goes off-script or asks about the bot during onboarding), "
    "\"safety_alert\" (self-harm, suicide, or severe crisis language). "
    "If the bot just asked a structured onboarding question (stress 1-5, notification time, job title, etc.) "
    "and the user responds briefly with a number, time, or short label, treat it as manager_flow. "
    "If current_state starts with \"onboarding\" and the user asks about the bot, privacy, or writes emotional text "
    "instead of answering the prompt, classify as onboarding_interruption. "
    "If you see clear suicidal ideation or self-harm intent, return safety_alert. "
    "Suggested UI: \"psychologist\" (user asks for a human specialist or describes being on the edge), "
    "\"settings\" (user wants to change notification time/frequency or turn off messages), "
    "\"plan_adjustment\" (tasks/plan feel too hard or need to be changed), \"none\" otherwise. "
    "Always return ONLY a valid JSON object with the exact keys: intent, manager, coach, safety, complexity_level, "
    "sentiment, suggested_ui, user_intent_summary, reasoning."
)

SYSTEM_PROMPT_COGNITIVE_ROUTER = """
### ROLE
You are the Cognitive Router for "Love Yourself". You only classify which specialist agent should respond.

CONSTRAINTS:
- NEVER speak to the user.
- NEVER add instructions for other agents.
- Output MUST be strict JSON.

---

### AVAILABLE AGENTS (STRICT ENUM)
`target_agent` MUST be one of:
- "safety" (crisis protocol)
- "onboarding" (profile setup flow)
- "manager" (settings & admin)
- "plan" (content & exercises)
- "coach" (empathy & support)

---

### PRIORITY HIERARCHY (LOGIC FLOW)
Evaluate rules in this order and stop at the first match.

1) SAFETY OVERRIDE
   Trigger: explicit or implied self-harm, suicide, severe crisis.
   Action: target_agent="safety", priority="high".

2) LOCKED STATES
   Trigger: current_state starts with "onboarding:" → target_agent="onboarding", priority="high";
            current_state starts with "plan_setup:" → target_agent="plan", priority="high".

3) MANAGER
   Trigger: commands about settings/admin (reminders, schedule, profile updates).
   Action: target_agent="manager", priority="normal".

4) PLAN
   Trigger: requests about exercises/tasks/plan adjustments.
   Action: target_agent="plan", priority="normal".

5) COACH DEFAULT
   Trigger: everything else.
   Action: target_agent="coach", priority="normal".

---

### OUTPUT FORMAT
Return ONLY this JSON structure (no markdown, no extra text):
{
  "target_agent": "safety | onboarding | manager | plan | coach",
  "priority": "high | normal"
}
"""

_ALLOWED_INTENTS = {
    "manager_flow",
    "coach_dialog",
    "onboarding_interruption",
    "safety_alert",
}

_ALLOWED_SENTIMENTS = {"neutral", "positive", "negative", "mixed"}
_ALLOWED_UI = {"none", "psychologist", "settings", "plan_adjustment"}

ALLOWED_TARGET_AGENTS = {"safety", "onboarding", "manager", "plan", "coach"}
ALLOWED_PRIORITIES = {"high", "normal"}


def _default_router_output(message_text: str | None) -> Dict[str, Any]:
    text = (message_text or "").strip()
    fallback_intent = "manager_flow" if text.startswith("/") else "coach_dialog"
    return {
        "intent": fallback_intent,
        "manager": 0.0,
        "coach": 0.0,
        "safety": 0.0,
        "complexity_level": 1,
        "sentiment": "neutral",
        "suggested_ui": "none",
        "user_intent_summary": "",
        "reasoning": "",
    }


def _extract_json_block(raw: str) -> str:
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    return match.group(0) if match else raw


def _merge_validated(base: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
    intent = data.get("intent")
    if intent in _ALLOWED_INTENTS:
        base["intent"] = intent

    try:
        manager = float(data.get("manager", base["manager"]))
        base["manager"] = max(0.0, min(1.0, manager))
    except (TypeError, ValueError):
        pass

    try:
        coach = float(data.get("coach", base["coach"]))
        base["coach"] = max(0.0, min(1.0, coach))
    except (TypeError, ValueError):
        pass

    try:
        safety = float(data.get("safety", base["safety"]))
        base["safety"] = max(0.0, min(1.0, safety))
    except (TypeError, ValueError):
        pass

    try:
        level = int(data.get("complexity_level", base["complexity_level"]))
        if level in {1, 2, 3}:
            base["complexity_level"] = level
    except (TypeError, ValueError):
        pass

    sentiment = data.get("sentiment")
    if isinstance(sentiment, str) and sentiment in _ALLOWED_SENTIMENTS:
        base["sentiment"] = sentiment

    suggested_ui = data.get("suggested_ui")
    if isinstance(suggested_ui, str) and suggested_ui in _ALLOWED_UI:
        base["suggested_ui"] = suggested_ui

    summary = data.get("user_intent_summary")
    if isinstance(summary, str):
        base["user_intent_summary"] = summary

    reasoning = data.get("reasoning")
    if isinstance(reasoning, str):
        base["reasoning"] = reasoning

    return base


async def route_message(context: dict) -> Dict[str, Any]:
    base = _default_router_output(context.get("message_text"))
    decision = base.copy()

    payload = {
        "current_state": context.get("current_state"),
        "last_bot_message": context.get("last_bot_message"),
        "recent_messages": context.get("recent_messages", []),
        "message_text": context.get("message_text"),
        "message_type": context.get("message_type"),
        "user_profile": context.get("user_profile"),
        "tg_id": context.get("tg_id"),
        "user_id": context.get("user_id"),
    }

    messages = [
        {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]

    try:
        response = await async_client.responses.create(
            model=settings.ROUTER_MODEL,
            input=messages,
            temperature=0.2,
            max_output_tokens=settings.MAX_TOKENS,
        )
        content = extract_output_text(response)
    except Exception as e:
        log_router_decision(
            {
                "event_type": "router_decision",
                "status": "llm_error",
                "error": str(e),
                "tg_id": context.get("tg_id"),
                "user_id": context.get("user_id"),
                "decision": decision,
            }
        )
        return decision

    if not content:
        log_router_decision(
            {
                "event_type": "router_decision",
                "status": "empty_response",
                "tg_id": context.get("tg_id"),
                "user_id": context.get("user_id"),
                "decision": decision,
            }
        )
        return decision

    try:
        parsed = json.loads(_extract_json_block(content))
    except Exception:
        log_router_decision(
            {
                "event_type": "router_decision",
                "status": "parse_error",
                "tg_id": context.get("tg_id"),
                "user_id": context.get("user_id"),
                "decision": decision,
            }
        )
        return decision

    if not isinstance(parsed, dict) or parsed.get("intent") not in _ALLOWED_INTENTS:
        log_router_decision(
            {
                "event_type": "router_decision",
                "status": "invalid_payload",
                "tg_id": context.get("tg_id"),
                "user_id": context.get("user_id"),
                "raw_output": parsed,
                "decision": decision,
            }
        )
        return decision

    decision = _merge_validated(base, parsed)
    log_router_decision(
        {
            "event_type": "router_decision",
            "status": "success",
            "tg_id": context.get("tg_id"),
            "user_id": context.get("user_id"),
            "decision": decision,
        }
    )

    return decision


async def cognitive_route_message(payload: dict) -> dict:
    """
    New cognitive router (v2).

    Expected payload from orchestrator:
      {
        "user_id": int,
        "message_text": str,
        "short_term_history": list[dict],   # [{"role": "user"|"bot", "text": "..."}]
        "profile_snapshot": dict,           # compressed user profile
        "current_state": str | None         # e.g. "onboarding:stress" or None
      }

    Returns:
      {
        "router_result": {
          "target_agent": "safety" | "onboarding" | "manager" | "plan" | "coach",
          "priority": "high" | "normal"
        },
        "router_meta": {
          "llm_prompt_tokens": int | None,
          "llm_response_tokens": int | None,
          "router_latency_ms": float | None
        }
      }
    """

    base = {
        "target_agent": "coach",
        "priority": "normal",
    }

    router_meta = {
        "llm_prompt_tokens": None,
        "llm_response_tokens": None,
        "router_latency_ms": None,
    }

    user_content = {
        "user_id": payload.get("user_id"),
        "message_text": payload.get("message_text") or "",
        "short_term_history": payload.get("short_term_history") or [],
        "profile_snapshot": payload.get("profile_snapshot") or {},
        "current_state": payload.get("current_state") or None,
    }

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_COGNITIVE_ROUTER},
        {"role": "user", "content": json.dumps(user_content, ensure_ascii=False)},
    ]

    try:
        t_start = time.monotonic()
        response = await async_client.responses.create(
            model=settings.ROUTER_MODEL,
            input=messages,
            temperature=0,
            response_format={"type": "json_object"},
            max_output_tokens=settings.MAX_TOKENS,
        )
        t_end = time.monotonic()

        if response is not None:
            usage = getattr(response, "usage", None)
            router_meta["llm_prompt_tokens"] = getattr(
                usage, "prompt_tokens", getattr(usage, "input_tokens", None)
            )
            router_meta["llm_response_tokens"] = getattr(
                usage, "completion_tokens", getattr(usage, "output_tokens", None)
            )

        router_meta["router_latency_ms"] = (t_end - t_start) * 1000
    except Exception as exc:  # noqa: PERF203
        log_router_decision(
            {
                "event_type": "cognitive_router_decision",
                "status": "llm_error",
                "error": str(exc),
                "user_id": payload.get("user_id"),
                "current_state": payload.get("current_state"),
                "decision": base,
            }
        )
        return {"router_result": base, "router_meta": router_meta}

    output = None
    try:
        output_list = getattr(response, "output", None)
        if output_list:
            content_blocks = getattr(output_list[0], "content", None)
            if content_blocks:
                output = getattr(content_blocks[0], "parsed", None) or getattr(
                    content_blocks[0], "input_json", None
                )
    except Exception:
        output = None

    if output is None:
        output_text = getattr(response, "output_text", None) or ""
        if output_text:
            try:
                output = json.loads(output_text)
            except Exception:
                log_router_decision(
                    {
                        "event_type": "cognitive_router_decision",
                        "status": "parse_error",
                        "user_id": payload.get("user_id"),
                        "current_state": payload.get("current_state"),
                        "decision": base,
                        "raw_output": output_text,
                        "router_meta": router_meta,
                    }
                )
                return {"router_result": base, "router_meta": router_meta}

    if output is None:
        log_router_decision(
            {
                "event_type": "cognitive_router_decision",
                "status": "empty_response",
                "user_id": payload.get("user_id"),
                "current_state": payload.get("current_state"),
                "decision": base,
                "router_meta": router_meta,
            }
        )
        return {"router_result": base, "router_meta": router_meta}

    if not isinstance(output, dict):
        log_router_decision(
            {
                "event_type": "cognitive_router_decision",
                "status": "parse_error",
                "user_id": payload.get("user_id"),
                "current_state": payload.get("current_state"),
                "decision": base,
                "raw_output": output,
                "router_meta": router_meta,
            }
        )
        return {"router_result": base, "router_meta": router_meta}

    target_agent = output.get("target_agent")
    priority = output.get("priority")

    if target_agent not in ALLOWED_TARGET_AGENTS:
        target_agent = base["target_agent"]

    if priority not in ALLOWED_PRIORITIES:
        priority = base["priority"]

    decision_payload = {
        "event_type": "cognitive_router_decision",
        "status": "success",
        "user_id": payload.get("user_id"),
        "current_state": payload.get("current_state"),
        "target_agent": target_agent,
        "priority": priority,
        "router_meta": router_meta,
    }
    log_router_decision(decision_payload)

    return {
        "router_result": {
            "target_agent": target_agent,
            "priority": priority,
        },
        "router_meta": router_meta,
    }
