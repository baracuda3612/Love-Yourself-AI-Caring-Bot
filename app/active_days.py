"""Utilities for user-defined active delivery days and task expiry."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import List, Optional

import pytz

ACTIVE_DAYS_DEFAULT: List[str] = ["MON", "TUE", "WED", "THU", "FRI"]

_WEEKDAY_MAP: dict[str, int] = {
    "MON": 0,
    "TUE": 1,
    "WED": 2,
    "THU": 3,
    "FRI": 4,
    "SAT": 5,
    "SUN": 6,
}

VALID_DAYS = frozenset(_WEEKDAY_MAP.keys())


class ActiveDaysError(ValueError):
    """Raised when active_days data is invalid."""


def normalize_active_days(raw: object) -> List[str]:
    """
    Validates and normalises a raw active_days value.
    Accepts a list of day strings (e.g. ["MON", "FRI"]).
    Returns sorted canonical list.  Falls back to default on any error.
    """
    if not isinstance(raw, list) or not raw:
        return ACTIVE_DAYS_DEFAULT.copy()
    result: List[str] = []
    for item in raw:
        if not isinstance(item, str):
            return ACTIVE_DAYS_DEFAULT.copy()
        upper = item.strip().upper()
        if upper not in VALID_DAYS:
            return ACTIVE_DAYS_DEFAULT.copy()
        if upper not in result:
            result.append(upper)
    if not result:
        return ACTIVE_DAYS_DEFAULT.copy()
    result.sort(key=lambda d: _WEEKDAY_MAP[d])
    return result


def resolve_active_days(profile: object) -> List[str]:
    """Returns active_days from profile, falling back to default."""
    raw = getattr(profile, "active_days", None) if profile else None
    return normalize_active_days(raw)


def is_active_day(d: date, active_days: List[str]) -> bool:
    """Returns True if the given date falls on an active delivery day."""
    weekday = d.weekday()  # 0=Mon … 6=Sun
    return any(_WEEKDAY_MAP[day] == weekday for day in active_days)


def next_active_date(from_date: date, active_days: List[str]) -> date:
    """Returns the next active date >= from_date (inclusive)."""
    candidate = from_date
    for _ in range(14):  # safety cap — never more than 14 iterations
        if is_active_day(candidate, active_days):
            return candidate
        candidate += timedelta(days=1)
    # Fallback: should never happen with a valid active_days list
    return from_date


def step_expires_at(
    scheduled_for: datetime,
    tz: pytz.BaseTzInfo,
) -> datetime:
    """
    Returns the expiry datetime for a task step.
    Rule: 23:59:59 on the same calendar day as scheduled_for in user's timezone.
    """
    local_dt = scheduled_for.astimezone(tz)
    end_of_day = local_dt.replace(hour=23, minute=59, second=59, microsecond=0)
    return end_of_day.astimezone(pytz.UTC)


def consecutive_active_days_gap(
    day_a: date,
    day_b: date,
    active_days: List[str],
) -> bool:
    """
    Returns True if day_a and day_b are consecutive in the active schedule
    (i.e. there are no active days between them).
    Used for streak calculation: FRI → MON is consecutive if SAT/SUN inactive.
    """
    if day_b <= day_a:
        return False
    cursor = day_a + timedelta(days=1)
    while cursor < day_b:
        if is_active_day(cursor, active_days):
            return False  # there is an active day in between → gap breaks streak
        cursor += timedelta(days=1)
    return True
