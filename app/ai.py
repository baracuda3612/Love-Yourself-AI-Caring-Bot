from openai import OpenAI
from app.config import OPENAI_API_KEY, MODEL, MAX_TOKENS, TEMPERATURE

client = OpenAI(api_key=OPENAI_API_KEY)

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
            model=MODEL,
            messages=messages,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
        )
        return resp.choices[0].message.content, _usage_dict(resp)
    except Exception as e:
        # ← ОТРИМАЄШ ПРЯМО В ЧАТІ, ЩО САМЕ ЛОМАЄТЬСЯ
        return f"ERR [{e.__class__.__name__}]: {e}", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

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
