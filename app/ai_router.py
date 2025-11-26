import asyncio
import json
import re
import time
from typing import Any, Dict

from openai import OpenAI

from app.config import settings

client = OpenAI(api_key=settings.OPENAI_API_KEY)

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
### SECTION 1: ROLE & IDENTITY
You are the Cognitive Router for "Love Yourself", an AI-based EAP wellbeing platform.
Your ONLY job is to analyze the incoming input, select the correct specialized agent, and provide a short, directive briefing.

CONSTRAINTS:
- You NEVER speak to the user.
- You NEVER generate content.
- Zero creativity. Temperature must be 0.
- Output MUST be a strict JSON object.

---

### SECTION 2: AVAILABLE AGENTS (STRICT ENUM)
Output `target_agent` MUST be one of:
1. "safety" (Crisis protocol)
2. "onboarding" (Profile setup flow)
3. "manager" (Settings & admin)
4. "plan" (Content & exercises)
5. "coach" (Empathy & support)

---

### SECTION 3: PRIORITY HIERARCHY (LOGIC FLOW)
Evaluate rules in this exact order (1 -> 5). Stop at the first match.

#### P1 — SAFETY (CRISIS OVERRIDE)
Trigger: Input contains signs of despair, self-harm, "I can't go on", severe dysfunction ("haven't eaten in days"), or physical danger.
Action:
- target_agent: "safety"
- priority: "high"
- agent_instruction: "Crisis detected. Execute safety protocol immediately. Validate pain and offer help button."
NOTE: This overrides ALL states, including onboarding.

#### P2 — LOCKED STATES (HARD FSM RULE)
Trigger: current_state starts with "onboarding:" OR "plan_setup:".
Action:
- if current_state starts with "onboarding:" → target_agent = "onboarding"
- if current_state starts with "plan_setup:" → target_agent = "plan"
- priority: "high"
- agent_instruction: "User is in a locked flow. Handle input or objection, then loop back to the current step."
CRITICAL: Ignore the user's intent to change settings or chat. Force them back to the flow unless P1.

#### P3 — MANAGER (OPERATIONAL)
Trigger: Explicit commands regarding settings/admin.
Examples: change time/date, set reminders, change goal, "show stats", "update profile".
Action:
- target_agent: "manager"
- priority: "normal"
- agent_instruction: "Execute operational command: [insert specific action]."

#### P4 — PLAN (CONTENT OPS)
Trigger: Requests regarding specific exercises, tasks, or plan content.
Examples: "Give me another exercise", "This is boring", "I did this yesterday", "New plan please".
Action:
- target_agent: "plan"
- priority: "normal"
- agent_instruction: "Modify or explain content based on user feedback."

#### P5 — COACH (DEFAULT)
Trigger: Everything else. Emotional sharing, venting, general conversation, requests for advice.
Action:
- target_agent: "coach"
- priority: "normal"
- agent_instruction: "Provide empathetic support/advice based on user context."

---

### SECTION 4: NLU INSTRUCTION RULES
Use these rules to generate the `agent_instruction` string.

1. Sarcasm/Humor:
If input has memes, hyperbole, or ironic insults (e.g., "департамент мусорки"),
ADD: "User is sarcastic. Handle playfully but stay on track."

2. Negative Emotion:
If frustration/anger is detected,
ADD: "User is frustrated. Validate emotion first, maintain containment."

3. Avoidance:
If user gives nonsense answers or avoids the question,
ADD: "User is avoiding. Gently redirect to the task."

4. Invalid Data:
If input format is wrong (e.g., "idk" for time),
ADD: "Invalid value. Politely ask for correct format."

5. Direct Command:
If the user clearly asks to execute an action,
ADD: "Direct execution required."

---

### SECTION 5: OUTPUT FORMAT
Return ONLY this JSON structure. No markdown. No quotes outside JSON. No explanations. No commentary. No additional fields.

{
  "target_agent": "safety | onboarding | manager | plan | coach (choose ONE)",
  "priority": "high | normal",
  "agent_instruction": "string"
}

Generate a concise, single-sentence directive for the agent (max 20 words).
The instruction must cover the required tone, user intent, and specific action.
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
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                temperature=0.2,
                max_tokens=200,
            ),
        )
        content = response.choices[0].message.content if response else None
    except Exception as e:
        print(f"[router_error] {e.__class__.__name__}: {e}")
        return base

    if not content:
        return base

    try:
        parsed = json.loads(_extract_json_block(content))
    except Exception:
        return base

    if not isinstance(parsed, dict):
        return base

    if parsed.get("intent") not in _ALLOWED_INTENTS:
        return base

    return _merge_validated(base, parsed)


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
          "priority": "high" | "normal",
          "agent_instruction": str
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
        "agent_instruction": "Handle user message according to platform rules.",
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
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.chat.completions.create(
                model=settings.MODEL,
                messages=messages,
                temperature=0,
                response_format={"type": "json_object"},
            ),
        )
        t_end = time.monotonic()

        if response is not None:
            usage = getattr(response, "usage", None)
            router_meta["llm_prompt_tokens"] = getattr(usage, "prompt_tokens", None)
            router_meta["llm_response_tokens"] = getattr(usage, "completion_tokens", None)

        router_meta["router_latency_ms"] = (t_end - t_start) * 1000
        content = response.choices[0].message.content if response else None
    except Exception:
        return {"router_result": base, "router_meta": router_meta}

    if not content:
        return {"router_result": base, "router_meta": router_meta}

    try:
        output = json.loads(content)
    except Exception:
        return {"router_result": base, "router_meta": router_meta}

    target_agent = output.get("target_agent")
    priority = output.get("priority")
    agent_instruction = output.get("agent_instruction")

    if target_agent not in ALLOWED_TARGET_AGENTS:
        target_agent = base["target_agent"]

    if priority not in ALLOWED_PRIORITIES:
        priority = base["priority"]

    if not isinstance(agent_instruction, str) or not agent_instruction.strip():
        agent_instruction = base["agent_instruction"]

    return {
        "router_result": {
            "target_agent": target_agent,
            "priority": priority,
            "agent_instruction": agent_instruction,
        },
        "router_meta": router_meta,
    }
