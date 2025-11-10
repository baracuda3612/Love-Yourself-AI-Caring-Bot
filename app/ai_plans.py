"""Utilities for generating multi-step AI plans and schedules."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dt_time
from typing import Any, Dict, Iterable, List, Optional

import pytz
from openai import OpenAI

from app.config import MAX_TOKENS, MODEL, OPENAI_API_KEY, TEMPERATURE

__all__ = ["generate_ai_plan"]

_client = OpenAI(api_key=OPENAI_API_KEY)

_PLAN_SYSTEM_PROMPT = (
    "Ти турботливий wellbeing-коуч. Створи покроковий план підтримки. "
    "Говори українською та враховуй пам'ять користувача. Відповідай лише валідним JSON."
    " Структура відповіді:\n"
    "{\n"
    "  \"plan_name\": \"лаконічна назва\",\n"
    "  \"goal\": \"підтверди початкову ціль\",\n"
    "  \"schedule\": [\n"
    "    {\"day\": <1-індексований номер дня>, \"time\": \"HH:MM\", \"message\": \"до 120 слів підтримки\"}\n"
    "  ]\n"
    "}\n"
    "Кожного дня має бути до {tasks_per_day} повідомлень. Уникай повторів і роби підтримувальний тон."
)

_PLAYBOOKS: Dict[str, Dict[str, Any]] = {
    "сон": {
        "plan_name": "М'який ритм сну",
        "messages": [
            "Створи вечірній ритуал: приглуши світло, вимкни екрани та зроби 5 хвилин дихальної практики перед сном.",
            "Запиши, що допомогло краще відпочити вчора. Попроси себе повторити це сьогодні та додай невелике розтягування.",
            "За годину до сну випий теплий трав'яний чай, прочитай кілька сторінок книжки та зафіксуй приємну думку перед сном.",
        ],
    },
    "куріння": {
        "plan_name": "Дихання без цигарок",
        "messages": [
            "Зроби паузу і відміть, що запускає бажання курити. Заміни ритуал на 5 глибоких вдихів та склянку води.",
            "Підготуй коробочку підтримки: жуйку, корисний перекус, підбадьорливе повідомлення собі на випадок тригера.",
            "Прогуляйся на свіжому повітрі без цигарки та відзнач, як відчувається тіло. Запиши один плюс від цієї зміни.",
        ],
    },
}

_DEFAULT_PLAN_NAME = "Турботливий план підтримки"
_DEFAULT_MESSAGES = [
    "Зроби глибокий вдих і випиши три речі, за які вдячний сьогодні. Поділися однією з них із близькою людиною.",
    "Знайди 15 хвилин на рух: прогулянка, розтягування або коротка танцювальна пауза. Відміть, що дає енергію.",
    "Під вечір підведи підсумок дня та заплануй маленьку приємність для себе на завтра.",
]

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True)
class _ParsedEntry:
    day: int
    message: str
    time: str
    uses_default_time: bool


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Try to parse a JSON object from a model response."""

    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_RE.search(text)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None


def _safe_timezone(name: str | None) -> pytz.BaseTzInfo:
    try:
        return pytz.timezone(name or "Europe/Kyiv")
    except pytz.UnknownTimeZoneError:
        return pytz.timezone("Europe/Kyiv")


def _coerce_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_time(value: Any, default: str) -> str:
    if isinstance(value, str):
        value = value.strip()
        if re.fullmatch(r"\d{1,2}:\d{2}", value):
            hour_str, minute_str = value.split(":", 1)
            try:
                hour_i, minute_i = int(hour_str), int(minute_str)
            except ValueError:
                return default
            if 0 <= hour_i < 24 and 0 <= minute_i < 60:
                return f"{hour_i:02d}:{minute_i:02d}"
    return default


def _adjust_time_for_index(base_time: str, index: int) -> str:
    hour, minute = [int(part) for part in base_time.split(":", 1)]
    new_hour = min(23, hour + max(0, index) * 2)
    return f"{new_hour:02d}:{minute:02d}"


def _localize_dt(tz: pytz.BaseTzInfo, base_date: datetime.date, hour: int, minute: int) -> datetime:
    naive = datetime.combine(base_date, dt_time(hour=hour, minute=minute))
    try:
        return tz.localize(naive, is_dst=None)
    except pytz.NonExistentTimeError:
        return tz.localize(naive + timedelta(hours=1), is_dst=True)
    except pytz.AmbiguousTimeError:
        return tz.localize(naive, is_dst=True)


def _schedule_datetime(
    now_local: datetime,
    tz: pytz.BaseTzInfo,
    day: int,
    time_str: str,
) -> datetime:
    hour, minute = [int(part) for part in time_str.split(":", 1)]
    day_index = max(1, day) - 1
    target_date = now_local.date() + timedelta(days=day_index)
    dt_local = _localize_dt(tz, target_date, hour, minute)
    if dt_local < now_local:
        dt_local += timedelta(days=1)
    return dt_local


def _extract_entries(data: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    for key in ("schedule", "steps", "entries", "days"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


def _parse_entries(raw_items: Iterable[Dict[str, Any]], default_time: str) -> List[_ParsedEntry]:
    parsed: List[_ParsedEntry] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        message = (item.get("message") or item.get("text") or "").strip()
        if not message:
            continue
        day = _coerce_int(
            item.get("day")
            or item.get("day_number")
            or item.get("day_index")
            or item.get("day_no"),
        )
        if day is None:
            offset = _coerce_int(
                item.get("offset_days")
                or item.get("day_offset")
                or item.get("days_from_now")
            )
            if offset is not None:
                day = offset + 1
        if day is None or day < 1:
            continue
        time_str = _coerce_time(
            item.get("time")
            or item.get("send_time")
            or item.get("hour"),
            default_time,
        )
        uses_default = time_str == default_time
        parsed.append(_ParsedEntry(day=day, message=message, time=time_str, uses_default_time=uses_default))
    parsed.sort(key=lambda entry: (entry.day, entry.time, entry.message))
    return parsed


def _find_playbook(goal: str | None) -> Optional[Dict[str, Any]]:
    if not goal:
        return None
    goal_lower = goal.lower()
    for keyword, playbook in _PLAYBOOKS.items():
        if keyword in goal_lower:
            return playbook
    return None


def _build_fallback_plan(
    goal: str,
    now_local: datetime,
    tz: pytz.BaseTzInfo,
    days: int,
    tasks_per_day: int,
    preferred_time: str,
) -> Dict[str, Any]:
    playbook = _find_playbook(goal)
    plan_name = (playbook or {}).get("plan_name", _DEFAULT_PLAN_NAME)
    messages = (playbook or {}).get("messages", _DEFAULT_MESSAGES)
    total_slots = max(1, days) * max(1, tasks_per_day)
    entries: List[Dict[str, Any]] = []
    per_day_index = defaultdict(int)
    for idx in range(total_slots):
        message = messages[idx % len(messages)]
        day = idx // max(1, tasks_per_day) + 1
        slot_index = per_day_index[day]
        per_day_index[day] += 1
        time_str = _adjust_time_for_index(preferred_time, slot_index)
        scheduled = _schedule_datetime(now_local, tz, day, time_str)
        entries.append({
            "day": day,
            "message": message,
            "time": time_str,
            "scheduled_for": scheduled,
        })
    return {
        "plan_name": plan_name,
        "goal": goal,
        "entries": entries,
        "source": "playbook",
    }


def generate_ai_plan(
    goal: str,
    days: int,
    tasks_per_day: int,
    preferred_hour: str,
    tz_name: str,
    memory: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Create a personalised multi-step plan and resolved schedule."""

    goal = (goal or "Підтримка добробуту").strip()
    days = max(1, days)
    tasks_per_day = max(1, tasks_per_day)
    preferred_hour = _coerce_time(preferred_hour, "21:00")
    tz = _safe_timezone(tz_name)
    now_local = datetime.now(tz)

    user_payload: Dict[str, Any] = {
        "goal": goal,
        "total_days": days,
        "tasks_per_day": tasks_per_day,
        "preferred_hour": preferred_hour,
        "timezone": tz.zone,
    }
    if memory:
        user_payload["memory"] = memory

    system_prompt = _PLAN_SYSTEM_PROMPT.format(tasks_per_day=tasks_per_day)
    user_prompt = (
        "Згенеруй wellbeing-план за структурою з системного повідомлення. "
        "Використай надані дані користувача:\n"
        f"{json.dumps(user_payload, ensure_ascii=False, indent=2)}"
    )

    content: Optional[str] = None
    try:
        response = _client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
        )
        if response.choices:
            content = response.choices[0].message.content
    except Exception:
        content = None

    data = _extract_json_object(content or "") if content else None
    if not data:
        return _build_fallback_plan(goal, now_local, tz, days, tasks_per_day, preferred_hour)

    response_goal = str(data.get("goal") or "").strip()
    if goal and (not response_goal or goal.lower() not in response_goal.lower()):
        return _build_fallback_plan(goal, now_local, tz, days, tasks_per_day, preferred_hour)

    raw_entries = _extract_entries(data)
    parsed_entries = _parse_entries(raw_entries, preferred_hour)
    filtered_entries: List[_ParsedEntry] = [entry for entry in parsed_entries if entry.day <= days]

    if not filtered_entries:
        return _build_fallback_plan(goal, now_local, tz, days, tasks_per_day, preferred_hour)

    per_day_counts = defaultdict(int)
    scheduled_entries: List[Dict[str, Any]] = []
    for entry in filtered_entries:
        per_day_index = per_day_counts[entry.day]
        if per_day_index >= tasks_per_day:
            continue
        per_day_counts[entry.day] += 1
        time_str = entry.time
        if entry.uses_default_time and per_day_index > 0:
            time_str = _adjust_time_for_index(preferred_hour, per_day_index)
        scheduled_entries.append({
            "day": entry.day,
            "message": entry.message,
            "time": time_str,
            "scheduled_for": _schedule_datetime(now_local, tz, entry.day, time_str),
        })

    if not scheduled_entries:
        return _build_fallback_plan(goal, now_local, tz, days, tasks_per_day, preferred_hour)

    plan_name = (data.get("plan_name") or goal or _DEFAULT_PLAN_NAME).strip()
    return {
        "plan_name": plan_name or _DEFAULT_PLAN_NAME,
        "goal": response_goal or goal,
        "entries": scheduled_entries,
        "source": "llm",
    }
