"""Utilities for parsing plan creation commands."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Optional

__all__ = ["PlanRequest", "parse_plan_request"]

_DEFAULT_DAYS = 7
_DEFAULT_HOUR = 21
_DEFAULT_MINUTE = 0
_DEFAULT_TASKS_PER_DAY = 1

_DURATION_PATTERNS = [
    re.compile(r"(?P<num>\d+)\s*[- ]?(?:денн(?:ий|іх|і|я|ого|ому|ими)?)", re.IGNORECASE),
    re.compile(r"(?:на|протягом|у\s*продовж)\s*(?P<num>\d+)\s*(?:дн(?:ів|і|я|ях))", re.IGNORECASE),
    re.compile(r"(?P<num>\d+)\s*(?:дн(?:ів|і|я|ях))", re.IGNORECASE),
    re.compile(r"(?P<num>\d+)\s*(?:тижн(?:ів|евий|еві|і|я|ях))", re.IGNORECASE),
]

_TIME_PATTERN = re.compile(
    r"(?:\b(?:о|в|у)\s*)?(?P<hour>\d{1,2})(?:[:\.](?P<minute>\d{2}))\b", re.IGNORECASE
)

_TASKS_PATTERN = re.compile(
    r"(?P<num>\d+)\s*(?:завдан(?:ь|ня)|крок(?:и|ів)|дій|практик|активност(?:і|ей)|вправ(?:и|)|пункт(?:и|ів))\s*(?:на\s*(?:день|добу)|щодня|щоденно)?",
    re.IGNORECASE,
)

_WEEK_KEYWORDS = {"тижні", "тижнів", "тиждень", "тижнях", "тижневий", "тижневі"}


@dataclass(frozen=True)
class PlanRequest:
    """Parsed request parameters for the /plan command.

    Examples
    --------
    >>> parse_plan_request("/plan 7-денний челендж підтримки о 22:00")
    PlanRequest(original_text='7-денний челендж підтримки о 22:00', goal='челендж підтримки', days=7, hour=22, minute=0, tasks_per_day=1)
    >>> parse_plan_request("/plan підтримка добробуту")
    PlanRequest(original_text='підтримка добробуту', goal='підтримка добробуту', days=7, hour=21, minute=0, tasks_per_day=1)
    """

    original_text: str
    goal: str
    days: int
    hour: int
    minute: int
    tasks_per_day: int

    @property
    def time_str(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"


def _to_int(value: Optional[str], default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_plan_request(text: str | None) -> PlanRequest:
    """Parse a raw `/plan` message into structured fields."""

    raw = (text or "").strip()
    body = raw
    if body.startswith("/plan"):
        body = body[5:]
        if body.startswith("@"):  # /plan@BotName support
            body = body.split(maxsplit=1)
            body = body[1] if len(body) > 1 else ""
        body = body.lstrip(" \t")
    original = body.strip()

    spans_to_remove: list[tuple[int, int]] = []
    days = _DEFAULT_DAYS
    hour = _DEFAULT_HOUR
    minute = _DEFAULT_MINUTE
    tasks = _DEFAULT_TASKS_PER_DAY

    # duration
    duration_match = None
    for pattern in _DURATION_PATTERNS:
        match = pattern.search(body)
        if match:
            duration_match = match
            break
    if duration_match:
        number = _to_int(duration_match.group("num"), _DEFAULT_DAYS)
        keyword = duration_match.group(0).lower()
        if any(k in keyword for k in _WEEK_KEYWORDS):
            number *= 7
        days = max(1, number)
        spans_to_remove.append(duration_match.span())

    # time (only consider first explicit time with minutes)
    time_match = _TIME_PATTERN.search(body)
    if time_match:
        hour_val = _to_int(time_match.group("hour"), _DEFAULT_HOUR)
        minute_val = _to_int(time_match.group("minute"), _DEFAULT_MINUTE)
        if 0 <= hour_val < 24 and 0 <= minute_val < 60:
            hour, minute = hour_val, minute_val
            spans_to_remove.append(time_match.span())

    # tasks per day
    tasks_match = _TASKS_PATTERN.search(body)
    if tasks_match:
        tasks_val = max(1, _to_int(tasks_match.group("num"), _DEFAULT_TASKS_PER_DAY))
        tasks = tasks_val
        spans_to_remove.append(tasks_match.span())

    cleaned_body = body
    if spans_to_remove:
        chars = list(cleaned_body)
        for start, end in spans_to_remove:
            for idx in range(start, end):
                if 0 <= idx < len(chars):
                    chars[idx] = " "
        cleaned_body = "".join(chars)

    goal = re.sub(r"\s+", " ", cleaned_body).strip()
    if not goal and original:
        goal = original

    return PlanRequest(
        original_text=original,
        goal=goal,
        days=days,
        hour=hour,
        minute=minute,
        tasks_per_day=tasks,
    )

