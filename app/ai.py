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
    "You are a STRICT classifier for onboarding user messages in a Telegram wellbeing bot.\n"
    "\n"
    "You NEVER generate user-facing text.\n"
    "You ONLY decide which intent label best describes the user's last message, given:\n"
    "- the current onboarding FSM state (State)\n"
    "- the onboarding question shown to the user (Context)\n"
    "- the raw user text (User text)\n"
    "\n"
    "Allowed intents (MUTUALLY EXCLUSIVE):\n"
    "- \"answer\"          – the text looks like a direct answer to the current onboarding question.\n"
    "- \"question\"        – the user is asking for information or clarity (has a question mark, or starts with why/what/how, etc.), not just answering.\n"
    "- \"skip_onboarding\" – the user clearly wants to skip, stop, or avoid onboarding (e.g. “skip”, “пропусти”, “не хочу це проходити”).\n"
    "- \"smalltalk\"       – casual chat, jokes, off-topic or emotional venting that is not a direct answer.\n"
    "- \"distress\"        – the user mentions wanting to die, self-harm, feeling on the edge, or very strong crisis language.\n"
    "- \"other\"           – anything that does not fit the classes above.\n"
    "\n"
    "State-specific hints:\n"
    "- If State contains \"waiting_goal\":\n"
    "  * \"answer\" is usually a short phrase like \"сон\", \"стрес\", \"продуктивність\", \"фокус на роботі\" etc.\n"
    "- If State contains \"waiting_stress\" or \"waiting_energy\":\n"
    "  * \"answer\" is a single integer from 1 to 5 (with or without spaces).\n"
    "  * Words like \"дуже сильно\", \"вигорів\", \"нормально\" are NOT \"answer\" – they are \"smalltalk\" or \"question\".\n"
    "- If State contains \"waiting_time\":\n"
    "  * \"answer\" is a time in HH:MM format (e.g. 09:00, 21:30), with hours 0–23 and minutes 0–59.\n"
    "- If State contains \"waiting_position\" or \"waiting_department\":\n"
    "  * \"answer\" is any short text describing a job role or department (e.g. \"Project Manager\", \"Sales\", \"IT\", \"HR\").\n"
    "- In ALL states:\n"
    "  * If the user clearly asks to skip or not do onboarding – use \"skip_onboarding\".\n"
    "  * If the message is mostly a question about the bot or onboarding – use \"question\".\n"
    "  * If the message includes strong crisis language (e.g. “не хочу жити”, “хочу померти”, “на межі”) – use \"distress\".\n"
    "\n"
    "Output format (MANDATORY):\n"
    "- Return ONLY valid JSON:\n"
    "  {\"intent\": \"<one_of: answer, question, skip_onboarding, smalltalk, other, distress>\"}\n"
    "- Do NOT add explanations, comments, or any other keys.\n"
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
