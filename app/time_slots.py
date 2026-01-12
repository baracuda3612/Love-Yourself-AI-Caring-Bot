"""Utilities for handling plan time slots and user time slot mappings."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Iterable, Optional

import pytz
from sqlalchemy.orm import Session, selectinload

from app.db import AIPlan, AIPlanDay, AIPlanStep, User, UserProfile

TIME_SLOTS = ("MORNING", "DAY", "EVENING")
DEFAULT_DAILY_TIME_SLOTS: Dict[str, str] = {
    "MORNING": "09:30",
    "DAY": "14:00",
    "EVENING": "21:00",
}


class TimeSlotError(ValueError):
    """Raised when time slot data is invalid."""


def normalize_time_slot(value: Any) -> str:
    if not isinstance(value, str):
        raise TimeSlotError("time_slot_not_string")
    normalized = value.strip().upper()
    if normalized not in TIME_SLOTS:
        raise TimeSlotError("time_slot_invalid")
    return normalized


def _parse_time(value: str) -> time:
    parts = value.split(":", 1)
    if len(parts) != 2:
        raise TimeSlotError("invalid_time_format")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise TimeSlotError("invalid_time_format") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise TimeSlotError("invalid_time_range")
    return time(hour=hour, minute=minute)


def normalize_daily_time_slots(raw: Any, *, require_all: bool) -> Dict[str, str]:
    if not isinstance(raw, dict):
        if require_all:
            raise TimeSlotError("daily_time_slots_invalid")
        return DEFAULT_DAILY_TIME_SLOTS.copy()
    normalized: Dict[str, str] = {}
    for key, value in raw.items():
        slot = normalize_time_slot(key)
        if not isinstance(value, str):
            raise TimeSlotError("daily_time_slots_invalid")
        parsed = _parse_time(value.strip())
        normalized[slot] = f"{parsed.hour:02d}:{parsed.minute:02d}"
    if require_all and any(slot not in normalized for slot in TIME_SLOTS):
        raise TimeSlotError("daily_time_slots_missing")
    for slot in TIME_SLOTS:
        normalized.setdefault(slot, DEFAULT_DAILY_TIME_SLOTS[slot])
    return normalized


def resolve_daily_time_slots(profile: Optional[UserProfile]) -> Dict[str, str]:
    raw = profile.daily_time_slots if profile else None
    try:
        return normalize_daily_time_slots(raw, require_all=False)
    except TimeSlotError:
        return DEFAULT_DAILY_TIME_SLOTS.copy()


def _normalize_timezone(name: Optional[str]) -> pytz.BaseTzInfo:
    try:
        return pytz.timezone(name or "UTC")
    except pytz.UnknownTimeZoneError:
        return pytz.UTC


def resolve_step_anchor(
    plan_start: datetime,
    day_number: int,
    scheduled_for: Optional[datetime] = None,
) -> datetime:
    if scheduled_for:
        return scheduled_for.astimezone(timezone.utc)
    start = plan_start
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    anchor = start + timedelta(days=max(day_number - 1, 0))
    return anchor.astimezone(timezone.utc)


def compute_scheduled_for(
    plan_start: datetime,
    day_number: int,
    time_slot: str,
    timezone_name: Optional[str],
    daily_time_slots: Dict[str, str],
    *,
    anchor_date: Optional[date] = None,
) -> datetime:
    slot = normalize_time_slot(time_slot)
    slot_time = _parse_time(daily_time_slots[slot])
    tz = _normalize_timezone(timezone_name)
    plan_start_local = plan_start
    if plan_start_local.tzinfo is None:
        plan_start_local = plan_start_local.replace(tzinfo=timezone.utc)
    plan_start_local = plan_start_local.astimezone(tz)
    if anchor_date is None:
        base_date = plan_start_local.date()
        first_slot_time = min(
            _parse_time(daily_time_slots[slot_key]) for slot_key in TIME_SLOTS
        )
        first_slot_naive = datetime.combine(base_date, first_slot_time)
        try:
            first_slot_local = tz.localize(first_slot_naive)
        except pytz.NonExistentTimeError:
            first_slot_local = tz.localize(first_slot_naive + timedelta(hours=1))
        except pytz.AmbiguousTimeError:
            first_slot_local = tz.localize(first_slot_naive, is_dst=False)
        if first_slot_local <= plan_start_local:
            base_date = base_date + timedelta(days=1)
    else:
        base_date = anchor_date
    target_date = base_date + timedelta(days=max(day_number - 1, 0))
    naive_local = datetime.combine(target_date, slot_time)
    try:
        local_dt = tz.localize(naive_local)
    except pytz.NonExistentTimeError:
        local_dt = tz.localize(naive_local + timedelta(hours=1))
    except pytz.AmbiguousTimeError:
        local_dt = tz.localize(naive_local, is_dst=False)
    return local_dt.astimezone(timezone.utc)


def iter_future_steps(
    plan: AIPlan,
    effective_from: datetime,
) -> Iterable[tuple[AIPlanDay, AIPlanStep]]:
    for day in plan.days:
        for step in day.steps:
            if step.is_completed or step.skipped:
                continue
            anchor = resolve_step_anchor(
                plan_start=plan.start_date or effective_from,
                day_number=day.day_number,
                scheduled_for=step.scheduled_for,
            )
            if anchor >= effective_from:
                yield day, step


def recompute_future_steps(
    user: User,
    plans: Iterable[AIPlan],
    daily_time_slots: Dict[str, str],
    *,
    effective_from: Optional[datetime] = None,
) -> list[int]:
    effective_from = effective_from or datetime.now(timezone.utc)
    updated_step_ids: list[int] = []
    for plan in plans:
        for day, step in iter_future_steps(plan, effective_from):
            plan_start = plan.start_date or effective_from
            anchor_date = resolve_step_date(
                plan_start=plan_start,
                day_number=day.day_number,
                scheduled_for=step.scheduled_for,
                timezone_name=user.timezone,
            )
            step.time_slot = normalize_time_slot(step.time_slot)
            step.scheduled_for = compute_scheduled_for(
                plan_start=plan_start,
                day_number=day.day_number,
                time_slot=step.time_slot,
                timezone_name=user.timezone,
                daily_time_slots=daily_time_slots,
                anchor_date=anchor_date,
            )
            updated_step_ids.append(step.id)
    return updated_step_ids


def resolve_step_date(
    *,
    plan_start: datetime,
    day_number: int,
    scheduled_for: Optional[datetime],
    timezone_name: Optional[str],
) -> date:
    tz = _normalize_timezone(timezone_name)
    if scheduled_for:
        return scheduled_for.astimezone(tz).date()
    start = plan_start
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    local_start = start.astimezone(tz)
    return local_start.date() + timedelta(days=max(day_number - 1, 0))


def update_user_time_slots(
    db: Session,
    user: User,
    raw_time_slots: Dict[str, str],
) -> list[int]:
    normalized = normalize_daily_time_slots(raw_time_slots, require_all=True)
    profile = user.profile
    if not profile:
        profile = UserProfile(user_id=user.id)
        db.add(profile)
        user.profile = profile
    profile.daily_time_slots = normalized

    plans = (
        db.query(AIPlan)
        .options(selectinload(AIPlan.days).selectinload(AIPlanDay.steps))
        .filter(
            AIPlan.user_id == user.id,
            AIPlan.status.in_(["active", "paused"]),
        )
        .all()
    )
    return recompute_future_steps(user, plans, normalized)
