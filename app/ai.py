from openai import OpenAI
from app.config import OPENAI_API_KEY, MODEL, MAX_TOKENS, TEMPERATURE

client = OpenAI(api_key=OPENAI_API_KEY)

SYSTEM_PROMPT = (
    "Ти короткий, емпатичний wellbeing-коуч українською. "
    "Надсилай 1 корисну думку/практику. 120–180 слів. "
    "Завершуй 1 практичним кроком і 1 питанням для рефлексії."
)

def _usage_dict(resp):
    # у нових версіях openai usage є завжди як об'єкт
    u = getattr(resp, "usage", None)
    if not u:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    return {
        "prompt_tokens": getattr(u, "prompt_tokens", 0),
        "completion_tokens": getattr(u, "completion_tokens", 0),
        "total_tokens": getattr(u, "total_tokens", 0),
    }

def generate_daily_message(user_profile: str, template_override: str | None = None) -> tuple[str, dict]:
    system_prompt = template_override or SYSTEM_PROMPT
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Профіль користувача: {user_profile}. Зроби щоденне нагадування/практику."},
        ],
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
    )
    msg = resp.choices[0].message.content
    return msg, _usage_dict(resp)

def answer_user_question(user_profile: str, question: str, template_override: str | None = None) -> tuple[str, dict]:
    system_prompt = template_override or SYSTEM_PROMPT
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Профіль користувача: {user_profile}"},
            {"role": "user", "content": f"Питання: {question}"},
        ],
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
    )
    msg = resp.choices[0].message.content
    return msg, _usage_dict(resp)

def generate_daily_message(profile: str, template: str):
    # тимчасова заглушка на 1 хвилину, щоб ізолювати БД/шедулер
    return "Тест: генерація працює ✅", {"prompt_tokens":0,"completion_tokens":
                                       }
