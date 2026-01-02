import json
import logging
import time
from typing import Any, Dict, Optional

from app.ai import async_client
from app.config import settings
from app.logging.router_logging import log_router_decision

logger = logging.getLogger(__name__)

# --- CONFIGURATION ---

ALLOWED_TARGET_AGENTS = {"safety", "onboarding", "manager", "plan", "coach"}
ALLOWED_PRIORITIES = {"high", "normal"}

# Use ROUTER_MODEL if exists, otherwise default to gpt-5-mini
ROUTER_MODEL = getattr(settings, "ROUTER_MODEL", "gpt-5-mini")

ROUTER_SYSTEM_PROMPT = """
### ROLE
You are the Cognitive Router for the "Love Yourself" app.
Your ONLY job is to analyze the user's message and determine which specialized agent should handle the response.

### OUTPUT FORMAT
You must return ONLY a valid JSON object in this exact format:
{
  "target_agent": "safety" | "onboarding" | "manager" | "plan" | "coach",
  "priority": "high" | "normal"
}

### STRICT RULES
- DO NOT output markdown blocks (no ```json). Just the raw JSON object.
- DO NOT add explanations, comments, or any text outside the JSON.
- DO NOT answer the user's question. Just classify it.

### ROUTING LOGIC

1. **SAFETY** (HIGHEST PRIORITY - overrides all states)
   - TRIGGER: Suicide thoughts, self-harm, loss of control, physical danger
   - EXAMPLES: "I want to die", "I'm cutting myself", "I can't control myself"
   - OUTPUT: {"target_agent": "safety", "priority": "high"}

2. **MANAGER** (System & Admin)
   - TRIGGER: Plan deletion, notification settings, reminder times, profile changes, data reset
   - EXAMPLES: "Delete my plan", "Change notification time", "Turn off reminders"
   - OUTPUT: {"target_agent": "manager", "priority": "normal"}

3. **PLAN** (Content & Exercises)
   - TRIGGER: Creating plan, modifying routine, requesting exercises/techniques
   - EXAMPLES: "Create a plan", "Change my routine", "Give me breathing exercise"
   - OUTPUT: {"target_agent": "plan", "priority": "normal"}

4. **COACH** (Default for ambiguous requests)
   - TRIGGER: General conversation, emotional support, vague requests
   - OUTPUT: {"target_agent": "coach", "priority": "normal"}
"""


def _detect_safety_crisis(message: str, history: list) -> bool:
    """Detect safety-critical signals in message or history."""
    safety_keywords = [
        "suicide", "kill myself", "end it", "no point living", "want to die",
        "cutting", "self-harm", "hurting myself", "bleeding",
        "losing control", "can't control", "out of control",
        "physical danger", "dangerous", "unsafe",
        "hopeless", "desperate", "no way out"
    ]
    
    text_to_check = message.lower()
    for msg in history:
        if isinstance(msg, str):
            text_to_check += " " + msg.lower()
        elif isinstance(msg, dict):
            content = msg.get("content", "") or msg.get("text", "")
            text_to_check += " " + content.lower()
    
    for keyword in safety_keywords:
        if keyword in text_to_check:
            return True
    
    return False


def _detect_manager_intent(message: str) -> bool:
    """Detect system/admin management intents."""
    manager_keywords = [
        "delete plan", "remove plan", "delete my", "remove my",
        "turn off", "disable", "stop notifications", "stop reminders",
        "change notification", "set reminder", "reminder time",
        "change profile", "edit profile", "update profile",
        "reset data", "clear data", "delete data"
    ]
    
    message_lower = message.lower()
    for keyword in manager_keywords:
        if keyword in message_lower:
            return True
    
    return False


def _detect_plan_intent(message: str) -> bool:
    """Detect plan/exercise/routine related intents."""
    plan_keywords = [
        "create plan", "make plan", "new plan", "build plan",
        "change routine", "modify routine", "update routine",
        "breathing exercise", "exercise", "technique",
        "add to plan", "update plan", "modify plan"
    ]
    
    message_lower = message.lower()
    for keyword in plan_keywords:
        if keyword in message_lower:
            return True
    
    return False


def _apply_hard_coded_rules(current_state: Optional[str], latest_message: str, short_history: list) -> Optional[Dict[str, str]]:
    """
    Apply hard-coded priority hierarchy BEFORE LLM call.
    Returns decision dict or None if LLM should decide.
    """
    current_state_str = str(current_state or "").strip()
    latest_message_str = str(latest_message or "").strip()
    
    # 1. SAFETY - Highest Priority (overrides all states)
    if _detect_safety_crisis(latest_message_str, short_history):
        return {"target_agent": "safety", "priority": "high"}
    
    # 2. FSM TUNNELS (LOCK-IN)
    # PLAN_FLOW tunnel
    if current_state_str.startswith("PLAN_FLOW"):
        return {"target_agent": "plan", "priority": "normal"}

    # ADAPTATION_FLOW tunnel
    if current_state_str == "ADAPTATION_FLOW":
        return {"target_agent": "plan", "priority": "normal"}
    
    # ONBOARDING tunnel
    if current_state_str.startswith("ONBOARDING"):
        return {"target_agent": "onboarding", "priority": "high"}
    
    # 3. ACTIVE (Open World)
    if current_state_str == "ACTIVE":
        # Manager overrides ACTIVE (but not safety)
        if _detect_manager_intent(latest_message_str):
            return {"target_agent": "manager", "priority": "normal"}
        # If about plan/exercises/routine change → plan
        if _detect_plan_intent(latest_message_str):
            return {"target_agent": "plan", "priority": "normal"}
        # Everything else → coach (HARD RULE, no LLM needed)
        return {"target_agent": "coach", "priority": "normal"}
    
    # 4. IDLE STATES
    if current_state_str in {"IDLE_NEW", "IDLE_ONBOARDED"}:
        # Manager allowed in IDLE states
        if _detect_manager_intent(latest_message_str):
            return {"target_agent": "manager", "priority": "normal"}
        # Default: plan start (coach NOT used as default)
        if _detect_plan_intent(latest_message_str):
            return {"target_agent": "plan", "priority": "normal"}
        return {"target_agent": "coach", "priority": "normal"}

    if current_state_str in {"IDLE_FINISHED", "IDLE_DROPPED", "IDLE_PLAN_ABORTED", "ACTIVE_PAUSED"}:
        if _detect_manager_intent(latest_message_str):
            return {"target_agent": "manager", "priority": "normal"}
        if _detect_plan_intent(latest_message_str):
            return {"target_agent": "plan", "priority": "normal"}
        return {"target_agent": "coach", "priority": "normal"}
    
    # If no hard rule applies, let LLM decide (only for truly ambiguous cases)
    # Note: PLAN_FLOW and ADAPTATION_FLOW are handled above and return plan immediately
    # Manager cannot override PLAN_FLOW/ADAPTATION_FLOW tunnels (they are atomic)
    return None


async def cognitive_route_message(payload: dict) -> dict:
    """
    Main routing function.
    Accepts payload from Orchestrator and returns decision + meta data.
    
    STRICT INPUT: Only reads user_id, current_state, latest_user_message, short_term_history
    """
    
    # Initialize tracking variables
    router_meta = {
        "llm_prompt_tokens": 0,
        "llm_response_tokens": 0,
        "router_latency_ms": 0.0,
    }
    
    # Default fallback decision
    decision = {
        "target_agent": "coach",
        "priority": "normal"
    }
    
    # STRICT INPUT: Only extract allowed fields
    user_id = payload.get("user_id")
    current_state = payload.get("current_state")
    latest_user_message = payload.get("latest_user_message") or payload.get("message_text", "")
    short_term_history = payload.get("short_term_history") or []
    
    try:
        # Apply hard-coded priority hierarchy FIRST
        hard_rule_decision = _apply_hard_coded_rules(
            current_state=current_state,
            latest_message=latest_user_message,
            short_history=short_term_history
        )
        
        if hard_rule_decision:
            # Hard rule matched - use it directly (no LLM call)
            decision = hard_rule_decision
            
            # Log hard rule decision
            log_router_decision({
                "event_type": "router_decision",
                "status": "success",
                "decision_source": "hard_rule",
                "user_id": user_id,
                "input_message": latest_user_message,
                "current_state": current_state,
                "decision": decision,
                "latency": 0.0,
                "llm_prompt_tokens": 0,
                "llm_response_tokens": 0,
            })
            
            return {
                "router_result": decision,
                "router_meta": router_meta
            }
        
        # No hard rule matched - use LLM for classification
        t_start = time.monotonic()
        
        # Build routing input (STRICT - only allowed fields)
        routing_input = {
            "user_id": user_id,
            "current_state": current_state,
            "short_term_history": _format_short_history(short_term_history),
            "latest_user_message": latest_user_message
        }
        
        # Prepare Messages
        messages = [
            {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(routing_input, ensure_ascii=False)}
        ]
        
        # Call LLM
        response = await async_client.chat.completions.create(
            model=ROUTER_MODEL,
            messages=messages,
            temperature=0,
            max_tokens=100,
            response_format={"type": "json_object"}
        )
        
        t_end = time.monotonic()
        router_meta["router_latency_ms"] = (t_end - t_start) * 1000
        
        # Extract Usage Stats
        if response.usage:
            router_meta["llm_prompt_tokens"] = response.usage.prompt_tokens
            router_meta["llm_response_tokens"] = response.usage.completion_tokens
        
        # Parse Output
        content = response.choices[0].message.content
        if not content:
            raise ValueError("Empty response from Router LLM")
        
        parsed_data = json.loads(content)
        
        # Validate output against allowed enums
        target = parsed_data.get("target_agent")
        priority = parsed_data.get("priority")
        
        if target in ALLOWED_TARGET_AGENTS:
            decision["target_agent"] = target
        
        if priority in ALLOWED_PRIORITIES:
            decision["priority"] = priority
        
        # Log success
        log_router_decision({
            "event_type": "router_decision",
            "status": "success",
            "decision_source": "llm",
            "user_id": user_id,
            "input_message": latest_user_message,
            "current_state": current_state,
            "decision": decision,
            "latency": router_meta["router_latency_ms"],
            "llm_prompt_tokens": router_meta["llm_prompt_tokens"],
            "llm_response_tokens": router_meta["llm_response_tokens"],
        })
        
    except json.JSONDecodeError as e:
        logger.error(f"[ROUTER ERROR] JSON parsing failed: {e}", exc_info=True)
        log_router_decision({
            "event_type": "router_failure",
            "status": "error",
            "error_type": "json_parse_error",
            "error": str(e),
            "user_id": user_id,
            "fallback": decision
        })
    
    except Exception as e:
        logger.error(f"[ROUTER ERROR] Failed to route message: {e}", exc_info=True)
        log_router_decision({
            "event_type": "router_failure",
            "status": "error",
            "error_type": type(e).__name__,
            "error": str(e),
            "user_id": user_id,
            "fallback": decision
        })
    
    return {
        "router_result": decision,
        "router_meta": router_meta
    }


def _format_short_history(history: Optional[list]) -> list:
    """Helper to format short-term history for the router."""
    if not history:
        return []
    
    formatted = []
    # Take only last 4 messages for routing context
    for msg in history[-4:]:
        if isinstance(msg, dict):
            role = msg.get("role", "")
            content = msg.get("content", "") or msg.get("text", "")
            if role and content:
                formatted.append({"role": role, "content": content[:200]})
        elif isinstance(msg, str):
            formatted.append({"role": "user", "content": msg[:200]})
    
    return formatted
