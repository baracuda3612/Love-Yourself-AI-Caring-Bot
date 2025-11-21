import json
import re
from enum import Enum

from openai import OpenAI
from app.config import settings

client = OpenAI(api_key=settings.OPENAI_API_KEY)

SYSTEM_PROMPT = (
    "Ти короткий, емпатичний wellbeing-коуч українською. "
    "Надсилай 1 корисну думку/практику. 120–180 слів. "
    "Завершуй 1 практичним кроком і 1 питанням для рефлексії."
)

def _usage_dict(resp):
    u = getattr(resp, "usage", None)
    if not u:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    return {
        "prompt_tokens": getattr(u, "prompt_tokens", 0),
        "completion_tokens": getattr(u, "completion_tokens", 0),
        "total_tokens": getattr(u, "total_tokens", 0),
    }

def _call_openai(messages):
    """Єдиний вхід у OpenAI, щоб показати точну причину фейлу всередині чату."""
    try:
        resp = client.chat.completions.create(
            model=settings.MODEL,
            messages=messages,
            max_tokens=settings.MAX_TOKENS,
            temperature=settings.TEMPERATURE,
        )
        return resp.choices[0].message.content, _usage_dict(resp)
    except Exception as e:
        # ← ОТРИМАЄШ ПРЯМО В ЧАТІ, ЩО САМЕ ЛОМАЄТЬСЯ
        return f"ERR [{e.__class__.__name__}]: {e}", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


class OnboardingIntent(str, Enum):
    ANSWER = "answer"
    QUESTION = "question"
    SKIP = "skip_onboarding"
    SMALLTALK = "smalltalk"
    OTHER = "other"
    DISTRESS = "distress"


_INTENT_PROMPT = (
    "You are a strict classifier for onboarding intents. "
    "Allowed intents: answer, question, skip_onboarding, smalltalk, other, distress. "
    "Rules: \n"
    "- 'answer' when the text matches the expected input for the current onboarding step.\n"
    "- 'question' for anything that looks like a request for info, has a question mark, or starts with why/what/how.\n"
    "- 'skip_onboarding' when the user asks to skip, stop, or avoid onboarding.\n"
    "- 'smalltalk' for casual chat, vents, or unrelated chatter.\n"
    "- 'distress' if the user mentions severe harm or wanting to die.\n"
    "- otherwise 'other'.\n"
    "Return only JSON: {\"intent\": <label>} with one of the allowed intents."
)


def _parse_intent_label(raw: str) -> str | None:
    try:
        maybe_json = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        text = maybe_json.group(0) if maybe_json else raw
        data = json.loads(text)
        if isinstance(data, dict):
            intent = data.get("intent")
            if isinstance(intent, str):
                return intent
    except Exception:
        pass
    match = re.search(r"intent\s*[:=]\s*\"?(\w+)\"?", raw, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def classify_onboarding_message(text: str, state_name: str, context: str | None = None) -> OnboardingIntent:
    """Classify onboarding message intent with a safe fallback to ANSWER."""

    normalized = (text or "").strip()
    lowered = normalized.lower()

    skip_keywords = [
        "пропусти",
        "скип",
        "skip",
        "не хочу",
        "не буду",
        "лень",
        "лінь",
        "пропусти онбординг",
        "без онбордингу",
    ]
    if any(k in lowered for k in skip_keywords):
        return OnboardingIntent.SKIP

    messages = [
        {"role": "system", "content": _INTENT_PROMPT},
        {
            "role": "user",
            "content": (
                f"State: {state_name}. Context: {context or 'n/a'}. "
                f"User text: {normalized or '[empty]'}"
            ),
        },
    ]

    try:
        content, _usage = _call_openai(messages)
    except Exception:
        return OnboardingIntent.ANSWER

    intent_label = _parse_intent_label(content or "")
    try:
        return OnboardingIntent(intent_label)  # type: ignore[arg-type]
    except Exception:
        return OnboardingIntent.ANSWER

def generate_daily_message(user_profile: str, template_override: str | None = None):
    system_prompt = template_override or SYSTEM_PROMPT
    return _call_openai([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Профіль користувача: {user_profile}. Зроби щоденне нагадування/практику."},
    ])

def answer_user_question(user_profile: str, question: str, template_override: str | None = None):
    system_prompt = template_override or SYSTEM_PROMPT
    return _call_openai([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Профіль користувача: {user_profile}"},
        {"role": "user", "content": f"Питання: {question}"},
    ])
