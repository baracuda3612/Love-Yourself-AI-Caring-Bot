"""Utilities for normalizing plan steps into a consistent draft structure."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from itertools import cycle, islice
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pytz

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


def _parse_time(value: str | None) -> Optional[time]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    parts = text.split(":", 1)
    if len(parts) != 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return time(hour=hour, minute=minute)
    return None


def _parse_preferred_hours(values: Sequence[str] | None) -> List[time]:
    parsed: List[time] = []
    for value in values or []:
        parsed_time = _parse_time(value)
        if parsed_time:
            parsed.append(parsed_time)
        if len(parsed) >= 10:
            break
    if parsed:
        return parsed
    fallback = _parse_time("21:00")
    return [fallback] if fallback else []


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
    return [str(msg).strip() for msg in messages if str(msg).strip()]


def normalize_plan_steps(
    plan_payload: Dict[str, Any] | None,
    *,
    goal: str,
    days: int,
    tasks_per_day: int,
    preferred_hour: str,
    preferred_hours: Optional[List[str]] = None,
    tz_name: str,
) -> List[Dict[str, Any]]:
    """Normalize raw plan steps into draft-ready payloads (deprecated legacy logic)."""

    payload = plan_payload or {}

    days_count = _coerce_positive_int(days, 1)
    requested_tasks = _coerce_positive_int(tasks_per_day, 1)

    tz = _safe_timezone(tz_name)
    preferred_times = _parse_preferred_hours(preferred_hours or [preferred_hour])
    if not preferred_times:
        preferred_times = [time(hour=21, minute=0)]
    tasks_count = min(max(requested_tasks, len(preferred_times)), 10)
    total_steps = days_count * tasks_count

    raw_steps = payload.get("steps") if isinstance(payload, dict) else []
    if not isinstance(raw_steps, list):
        raw_steps = []

    messages = _ensure_messages(_extract_messages(raw_steps))
    if not messages:
        return []

    repeated_messages = list(islice(cycle(messages), total_steps))

    now_local = datetime.now(tz)
    start_date = now_local.date()
    first_time = min(preferred_times)
    first_naive = datetime.combine(start_date, first_time)
    try:
        first_local = tz.localize(first_naive)
    except pytz.NonExistentTimeError:
        first_local = tz.localize(first_naive + timedelta(hours=1))
    except pytz.AmbiguousTimeError:
        first_local = tz.localize(first_naive, is_dst=False)

    if first_local <= now_local:
        start_date = start_date + timedelta(days=1)

    normalized: List[Dict[str, Any]] = []
    message_iter = iter(repeated_messages)

    for day_index in range(days_count):
        target_date = start_date + timedelta(days=day_index)
        for slot_index in range(tasks_count):
            try:
                message = next(message_iter)
            except StopIteration:  # pragma: no cover - defensive
                break
            if not message:
                continue

            slot_time = preferred_times[slot_index % len(preferred_times)]
            naive_local = datetime.combine(target_date, slot_time)
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
                    "day_index": day_index,
                    "slot_index": slot_index,
                    "time": f"{slot_time.hour:02d}:{slot_time.minute:02d}",
                    "message": message,
                    "proposed_for": proposed_utc,
                    "status": "pending",
                    "job_id": None,
                    "scheduled_for": None,
                }
            )

    return normalized
