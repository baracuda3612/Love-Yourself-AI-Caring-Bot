"""Utilities for generating multi-step AI plans and schedules."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dt_time
from typing import Any, Dict, Iterable, List, Tuple

import pytz
from openai import OpenAI

from app.config import MAX_TOKENS, MODEL, OPENAI_API_KEY, TEMPERATURE

__all__ = ["generate_ai_plan"]

_client = OpenAI(api_key=OPENAI_API_KEY)

_PLAN_SYSTEM_PROMPT = (
    "Ти турботливий wellbeing-коуч. Створи чіткий план з коротких щоденних кроків. "
    "Усі відповіді — українською. Врахуй індивідуальний запит і пам'ять користувача (якщо є). "
    "Формат відповіді — лише валідний JSON без пояснень." 
    " Використовуй структуру: {\n"
    "  \"plan_name\": \"креативна назва\",\n"
    "  \"steps\": [\n"
    "    {\"message\": \"коротке підтримувальне повідомлення до 120 слів\","
    " \"offset_days\": <int від 0>, \"time\": \"HH:MM\" }\n"
    "  ]\n"
    "}. \n"
    "offset_days означає через скільки днів після сьогодні відправити повідомлення "
    "(0 — сьогодні, 1 — завтра). time — 24-годинний формат у часовому поясі користувача."
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class _RawStep:
    message: str
    offset_days: int
    time: str


def _extract_json_object(text: str) -> Dict[str, Any] | None:
    """Try to parse a JSON object from a model response."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_RE.search(text)
        if not match:
            return None
        candidate = match.group(0)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            return None


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_time(value: Any, default: str = "09:00") -> str:
    if not isinstance(value, str):
        return default
    value = value.strip()
    if re.fullmatch(r"\d{1,2}:\d{2}", value):
        hour, minute = value.split(":", 1)
        try:
            hour_i, minute_i = int(hour), int(minute)
        except ValueError:
            return default
        if 0 <= hour_i < 24 and 0 <= minute_i < 60:
            return f"{hour_i:02d}:{minute_i:02d}"
    return default


def _parse_steps(data: Dict[str, Any]) -> Iterable[_RawStep]:
    steps = data.get("steps")
    if not isinstance(steps, list):
        return []
    parsed: List[_RawStep] = []
    for item in steps:
        if not isinstance(item, dict):
            continue
        message = (item.get("message") or "").strip()
        if not message:
            continue
        offset_days = _coerce_int(
            item.get("offset_days", item.get("day_offset", item.get("days_from_now", 0)))
        )
        send_time = _coerce_time(
            item.get("time")
            or item.get("send_time")
            or item.get("send_at")
            or item.get("scheduled_time")
        )
        parsed.append(_RawStep(message=message, offset_days=max(0, offset_days), time=send_time))
    return parsed


def _resolve_datetime(
    now_local: datetime,
    tz: pytz.BaseTzInfo,
    step: _RawStep,
    explicit: Any,
) -> datetime:
    if isinstance(explicit, str):
        candidate = explicit.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            dt = None
        if dt is not None:
            if dt.tzinfo is None:
                dt = tz.localize(dt)
            else:
                dt = dt.astimezone(tz)
            if dt < now_local:
                dt = now_local + timedelta(minutes=1)
            return dt
    base_date = now_local.date() + timedelta(days=step.offset_days)
    hour, minute = [int(part) for part in step.time.split(":", 1)]
    naive_dt = datetime.combine(base_date, dt_time(hour=hour, minute=minute))
    try:
        dt_local = tz.localize(naive_dt, is_dst=None)
    except pytz.NonExistentTimeError:
        dt_local = tz.localize(naive_dt + timedelta(hours=1), is_dst=True)
    except pytz.AmbiguousTimeError:
        dt_local = tz.localize(naive_dt, is_dst=True)
    if dt_local <= now_local:
        dt_local += timedelta(days=1)
    return dt_local


def _fallback_plan(now_local: datetime) -> Tuple[str, List[Dict[str, Any]]]:
    messages = [
        "Зроби глибокий вдих. Запиши 3 речі, за які вдячний сьогодні. Поділися ними з близькою людиною.",
        "Знайди 15 хвилин для руху. Прислухайся до тіла і відзнач, що дає енергію.",
        "Перед сном випиши думки, які заважають розслабитись. Запропонуй собі лагідну відповідь на кожну.",
    ]
    steps: List[Dict[str, Any]] = []
    for idx, msg in enumerate(messages):
        dt_local = now_local + timedelta(days=idx + 1)
        dt_local = dt_local.replace(hour=9, minute=0, second=0, microsecond=0)
        steps.append({"message": msg, "scheduled_for": dt_local})
    return "Турботливий план підтримки", steps


def generate_ai_plan(
    description: str,
    memory_profile: Dict[str, Any] | None = None,
    timezone: str = "Europe/Kyiv",
) -> Tuple[str, List[Dict[str, Any]]]:
    """Create a personalised multi-step plan and resolved schedule."""

    try:
        tz = pytz.timezone(timezone or "Europe/Kyiv")
    except pytz.UnknownTimeZoneError:
        tz = pytz.timezone("Europe/Kyiv")
    now_local = datetime.now(tz)

    user_message = (
        "Зроби wellbeing-план за запитом: "
        f"{description}.\nЧасовий пояс користувача: {timezone or 'Europe/Kyiv'}."
    )
    if memory_profile:
        profile_json = json.dumps(memory_profile, ensure_ascii=False)
        user_message += f"\nДоступний профіль користувача: {profile_json}."

    content = None
    try:
        response = _client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": _PLAN_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
        )
        content = response.choices[0].message.content if response.choices else None
    except Exception:
        content = None

    data = _extract_json_object(content or "") if content else None

    if not data:
        return _fallback_plan(now_local)

    plan_name = (data.get("plan_name") or description or "AI План").strip()
    raw_steps = list(_parse_steps(data))

    if not raw_steps:
        return _fallback_plan(now_local)

    resolved_steps: List[Dict[str, Any]] = []
    original_steps = data.get("steps") if isinstance(data.get("steps"), list) else []
    for idx, raw in enumerate(raw_steps):
        explicit_dt = None
        if idx < len(original_steps) and isinstance(original_steps[idx], dict):
            explicit_dt = original_steps[idx].get("scheduled_for")
        dt_local = _resolve_datetime(now_local, tz, raw, explicit_dt)
        resolved_steps.append({"message": raw.message, "scheduled_for": dt_local})

    return plan_name or "AI План", resolved_steps
