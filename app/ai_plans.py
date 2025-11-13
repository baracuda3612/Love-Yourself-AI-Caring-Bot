"""Utilities for generating structured AI plans."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional

import pytz
from openai import OpenAI

from app.config import settings

__all__ = ["generate_ai_plan"]


_client = OpenAI(api_key=settings.OPENAI_API_KEY)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

_SYSTEM_PROMPT_TEMPLATE = (
    """Ти створюєш індивідуальний план дій українською мовою.\n"
    "ПОВЕРТАЙ ВИКЛЮЧНО JSON без тексту навколо.\n"
    "Формат обов’язковий:\n"
    "{{\n"
    "    \"plan_name\": \"рядок\",\n"
    "    \"steps\": [\n"
    "        {{\n"
    "            \"day\": <номер дня>,\n"
    "            \"message\": \"текст завдання\",\n"
    "            \"scheduled_for\": \"ISO8601 дата (опціонально)\"\n"
    "        }}\n"
    "    ]\n"
    "}}\n"
    "Правила:\n"
    "- Без описів, без пояснень, без ```json блоків.\n"
    "- Якщо не впевнений, все одно повертай валідний JSON із хоча б одним кроком.\n"
    "- Пиши стисло, практично.\n"
    "Мета: {goal}\n"
    "Тривалість: {days} днів\n"
    "Завдань на день: {tasks_per_day}\n"
    "Час нагадування: {preferred_hour} ({tz})\n"
    "USER_MEMORY: {memory}\n"""
)


PLAYBOOKS: Dict[str, List[str]] = {
    "сон": [
        "Створи вечірній ритуал: приглуши світло та вимкни екрани за годину до сну.",
        "Запиши, що допомогло краще відпочити вчора, і повтори це сьогодні.",
        "За годину до сну випий теплий трав'яний чай і зроби 5 хвилин розслаблення.",
    ],
    "куріння": [
        "Відстеж тригери бажання закурити та заміни їх на 5 глибоких вдихів і склянку води.",
        "Підготуй підтримувальний набір: жуйку, корисний перекус і коротке підбадьорення.",
        "Зроби прогулянку без цигарки та запиши один плюс від цього досвіду.",
    ],
    "тривога": [
        "Зроби техніку заземлення 5-4-3-2-1 і назви вголос, що допомагає відчувати контроль.",
        "Сплануй коротку приємність на вечір і закріпи її у календарі.",
        "Напиши собі підтримувальне повідомлення на випадок напруги.",
    ],
}

_DEFAULT_PLAYBOOK = [
    "Зроби глибокий вдих і запиши три речі, за які вдячний сьогодні.",
    "Присвяти 15 хвилин руху: прогулянка, розтягування або легкий танець.",
    "Перед сном підсумуй день і заплануй маленьку радість на завтра.",
]


def _safe_timezone(name: Optional[str]) -> pytz.BaseTzInfo:
    try:
        return pytz.timezone(name or "Europe/Kyiv")
    except pytz.UnknownTimeZoneError:
        return pytz.timezone("Europe/Kyiv")


def _format_memory(memory: Optional[Dict[str, Any]]) -> str:
    if not memory:
        return "порожньо"
    try:
        return json.dumps(memory, ensure_ascii=False)
    except (TypeError, ValueError):
        return "недоступно"


def _coerce_positive_int(value: Any, default: int = 1) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return max(1, default)


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    match = _JSON_RE.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        return None
    return data


def _coerce_steps(payload: Dict[str, Any], goal: str) -> Optional[Dict[str, Any]]:
    raw_steps: Iterable[Any] = payload.get("steps")
    if not isinstance(raw_steps, list):
        raw_steps = []

    steps: List[Dict[str, Any]] = []
    for item in raw_steps:
        if not isinstance(item, dict):
            continue
        message = str(item.get("message") or "").strip()
        if not message:
            continue
        day_value = item.get("day")
        try:
            day_int = int(day_value)
        except (TypeError, ValueError):
            continue
        if day_int < 1:
            continue
        step: Dict[str, Any] = {"day": day_int, "message": message}
        scheduled_for = item.get("scheduled_for")
        if isinstance(scheduled_for, str):
            scheduled_str = scheduled_for.strip()
            if scheduled_str:
                step["scheduled_for"] = scheduled_str
        steps.append(step)

    if not steps:
        return None

    steps.sort(key=lambda x: (x["day"], x.get("scheduled_for", "")))
    plan_name = str(payload.get("plan_name") or "").strip() or f"План для {goal}"
    return {"plan_name": plan_name, "steps": steps}


def _select_playbook(goal: str) -> List[str]:
    goal_lower = goal.lower()
    for keyword, messages in PLAYBOOKS.items():
        if keyword in goal_lower:
            return messages
    return _DEFAULT_PLAYBOOK


def _build_fallback_plan(goal: str) -> Dict[str, Any]:
    messages = _select_playbook(goal)
    if not messages:
        messages = _DEFAULT_PLAYBOOK or ["Зроби маленький крок турботи про себе."]
    steps = [
        {"day": index + 1, "message": message}
        for index, message in enumerate(messages)
    ]
    if not steps:
        steps = [{"day": 1, "message": "Зроби маленький крок турботи про себе."}]
    return {"plan_name": f"План для {goal}", "steps": steps}


def _request_plan(messages: List[Dict[str, str]]) -> Optional[Dict[str, Any]]:
    try:
        response = _client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.2,
            max_tokens=1200,
        )
    except Exception:
        return None

    if not getattr(response, "choices", None):
        return None

    content = response.choices[0].message.content if response.choices else None
    if not isinstance(content, str):
        return None
    return _extract_json_object(content)


def generate_ai_plan(
    goal: str,
    days: int,
    tasks_per_day: int,
    preferred_hour: str,
    tz_name: str,
    memory: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create an AI plan that always conforms to the strict schema."""

    goal = (goal or "Підтримка добробуту").strip() or "Підтримка добробуту"
    days = _coerce_positive_int(days, 1)
    tasks_per_day = _coerce_positive_int(tasks_per_day, 1)
    preferred_hour = (preferred_hour or "21:00").strip() or "21:00"
    tz = _safe_timezone(tz_name)

    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        goal=goal,
        days=days,
        tasks_per_day=tasks_per_day,
        preferred_hour=preferred_hour,
        tz=tz.zone,
        memory=_format_memory(memory),
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Згенеруй план під мету: {goal}"},
    ]

    data = _request_plan(messages)

    if data is None:
        retry_messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": "Ти не дотримався інструкцій. Відповідай ВИКЛЮЧНО валідним JSON за схемою. Без пояснень.",
            },
        ]
        data = _request_plan(retry_messages)

    if not isinstance(data, dict):
        return _build_fallback_plan(goal)

    plan = _coerce_steps(data, goal)
    if plan is None:
        return _build_fallback_plan(goal)

    return plan

