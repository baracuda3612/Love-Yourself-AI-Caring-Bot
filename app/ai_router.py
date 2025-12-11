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

ROUTER_SYSTEM_PROMPT = """
### ROLE
You are the Cognitive Router for the "Love Yourself" app.
Your ONLY job is to analyze the user's message and current state to determine which specialized agent should handle the response.

### OUTPUT FORMAT
You must return a valid JSON object in the following format:
{
  "target_agent": "safety" | "onboarding" | "manager" | "plan" | "coach",
  "priority": "high" | "normal"
}

### AGENT DEFINITIONS & ROUTING LOGIC

1. **safety** (Crisis Protocol) [PRIORITY: HIGH]
   - TRIGGER: User mentions suicide, self-harm, extreme hopelessness, acute psychological crisis, or physical danger.
   - EXAMPLES: "I want to die", "I'm cutting myself", "There is no point in living".
   - ACTION: Immediate reroute to safety.

2. **onboarding** (Profile Setup) [PRIORITY: HIGH]
   - TRIGGER: The `current_state` provided in input starts with "onboarding:".
   - EXAMPLES: User answering setup questions (name, age, goals) while in onboarding mode.
   - EXCEPTION: If user signals crisis during onboarding -> Route to **safety**.

3. **manager** (System & Admin) [PRIORITY: NORMAL]
   - TRIGGER: User asks to change settings, set reminders, adjust schedules, change notification times, or delete data.
   - EXAMPLES: "Change notification time to 9 AM", "Delete my profile", "Turn off reminders", "Stop messaging me".

4. **plan** (Content & Exercises) [PRIORITY: NORMAL]
   - TRIGGER: User asks to CREATE a plan, MODIFY a routine, or asks for specific exercises/techniques (breathing, CBT logs).
   - EXAMPLES: "Create a sleep plan", "Give me a breathing exercise", "I want to change my morning routine", "Add meditation to my plan".

5. **coach** (Empathy & Support) [PRIORITY: NORMAL] - **DEFAULT**
   - TRIGGER: General conversation, emotional venting, asking for advice, feeling lonely, casual chat, or vague requests.
   - EXAMPLES: "I feel sad", "Hello", "What should I do?", "I'm tired", "Tell me a joke".
   - NOTE: If the request is ambiguous, default to **coach**.

### IMPORTANT RULES
- DO NOT output markdown blocks (like ```json). Just the raw JSON object.
- DO NOT answer the user's question. Just classify it.
- Prioritize SAFETY above all else.
"""

async def cognitive_route_message(payload: dict) -> dict:
    """
    Main routing function.
    Accepts payload from Orchestrator and returns decision + meta data.
    """
    
    # 1. Prepare Data
    user_id = payload.get("user_id")
    message_text = payload.get("message_text", "")
    current_state = payload.get("current_state")
    
    # Build a clean context for the LLM
    routing_input = {
        "user_id": user_id,
        "current_state": current_state,
        "last_messages": _format_short_history(payload.get("short_term_history")),
        "latest_user_message": message_text
    }

    # 2. Prepare Messages
    messages = [
        {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(routing_input, ensure_ascii=False)}
    ]

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

    try:
        t_start = time.monotonic()
        
        # 3. Call OpenAI (using Chat Completions, NOT Responses)
        # We use a cheaper model for routing (e.g. gpt-4o-mini) defined in settings
        response = await async_client.chat.completions.create(
            model=settings.ROUTER_MODEL,  # Ensure this is set in config (e.g., "gpt-4o-mini")
            messages=messages,
            temperature=0.0,  # Deterministic for routing
            max_tokens=100,
            response_format={"type": "json_object"}  # Force JSON output
        )
        
        t_end = time.monotonic()
        router_meta["router_latency_ms"] = (t_end - t_start) * 1000

        # 4. Extract Usage Stats
        if response.usage:
            router_meta["llm_prompt_tokens"] = response.usage.prompt_tokens
            router_meta["llm_response_tokens"] = response.usage.completion_tokens

        # 5. Parse Output
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
            "user_id": user_id,
            "input_message": message_text,
            "decision": decision,
            "latency": router_meta["router_latency_ms"]
        })

    except Exception as e:
        logger.error(f"[ROUTER ERROR] Failed to route message: {e}", exc_info=True)
        # Log failure but return default 'coach' so the bot doesn't crash
        log_router_decision({
            "event_type": "router_failure",
            "status": "error",
            "error": str(e),
            "user_id": user_id,
            "fallback": decision
        })

    return {
        "router_result": decision,
        "router_meta": router_meta
    }


def _format_short_history(history: Optional[list]) -> list:
    """Helper to simplify history for the router (save tokens)."""
    if not history:
        return []
    
    simplified = []
    # Take only last 4 messages for routing context to save costs/latency
    for msg in history[-4:]: 
        role = msg.get("role")
        content = msg.get("content")
        if role and content:
            simplified.append(f"{role}: {content[:200]}") # Truncate long messages
    return simplified
