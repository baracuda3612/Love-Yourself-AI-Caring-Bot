"""Utilities for normalizing plan steps into a consistent draft structure."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from itertools import cycle, islice
from typing import Any, Dict, Iterable, List

import pytz

from app.ai_plans import PLAYBOOKS, _DEFAULT_PLAYBOOK

__all__ = ["normalize_plan_steps"]


def _coerce_positive_int(value: Any, default: int = 1) -> int:
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return max(1, default)
    return max(1, coerced)


def _safe_timezone(name: str | None) -> pytz.BaseTzInfo:
    try:
        return pytz.timezone(name or "Europe/Kyiv")
    except (pytz.UnknownTimeZoneError, AttributeError):
        return pytz.timezone("Europe/Kyiv")


def _parse_preferred_hour(value: str | None) -> time:
    fallback_hour, fallback_minute = 21, 0
    if isinstance(value, str):
        text = value.strip()
        if text:
            parts = text.split(":", 1)
            if len(parts) == 2:
                try:
                    hour = int(parts[0])
                    minute = int(parts[1])
                except ValueError:
                    hour = minute = None  # trigger fallback
                else:
                    if 0 <= hour <= 23 and 0 <= minute <= 59:
                        return time(hour=hour, minute=minute)
    return time(hour=fallback_hour, minute=fallback_minute)


def _choose_playbook(goal: str) -> List[str]:
    goal_lower = (goal or "").lower()
    for keyword, messages in PLAYBOOKS.items():
        if keyword in goal_lower:
            return list(messages)
    return list(_DEFAULT_PLAYBOOK)


def _extract_messages(raw_steps: Iterable[Any]) -> List[str]:
    messages: List[str] = []
    for item in raw_steps:
        if not isinstance(item, dict):
            continue
        message = str(item.get("message") or "").strip()
        if message:
            messages.append(message)
    return messages


def _ensure_messages(messages: List[str]) -> List[str]:
    cleaned = [str(msg).strip() for msg in messages if str(msg).strip()]
    if cleaned:
        return cleaned
    return ["Зроби маленький крок турботи про себе."]


def normalize_plan_steps(
    plan_payload: Dict[str, Any] | None,
    *,
    goal: str,
    days: int,
    tasks_per_day: int,
    preferred_hour: str,
    tz_name: str,
) -> List[Dict[str, Any]]:
    """Normalize raw plan steps into draft-ready payloads."""

    payload = plan_payload or {}

    days_count = _coerce_positive_int(days, 1)
    tasks_count = _coerce_positive_int(tasks_per_day, 1)
    total_steps = days_count * tasks_count

    tz = _safe_timezone(tz_name)
    reminder_time = _parse_preferred_hour(preferred_hour)

    raw_steps = payload.get("steps") if isinstance(payload, dict) else []
    if not isinstance(raw_steps, list):
        raw_steps = []

    messages = _extract_messages(raw_steps)
    if not messages:
        messages = _choose_playbook(goal or "Підтримка добробуту")
    messages = _ensure_messages(messages)

    repeated_messages = list(islice(cycle(messages), total_steps))

    now_local = datetime.now(tz)
    start_date = now_local.date()

    normalized: List[Dict[str, Any]] = []
    message_iter = iter(repeated_messages)

    for day_index in range(days_count):
        target_date = start_date + timedelta(days=day_index)
        for _ in range(tasks_count):
            try:
                message = next(message_iter)
            except StopIteration:  # pragma: no cover - defensive
                break
            if not message:
                continue

            naive_local = datetime.combine(target_date, reminder_time)
            try:
                local_dt = tz.localize(naive_local)
            except pytz.NonExistentTimeError:
                adjusted = naive_local + timedelta(hours=1)
                local_dt = tz.localize(adjusted)
            except pytz.AmbiguousTimeError:
                local_dt = tz.localize(naive_local, is_dst=False)

            proposed_utc = local_dt.astimezone(pytz.UTC)

            normalized.append(
                {
                    "day": day_index + 1,
                    "message": message,
                    "proposed_for": proposed_utc,
                    "status": "pending",
                    "job_id": None,
                    "scheduled_for": None,
                }
            )

    return normalized

