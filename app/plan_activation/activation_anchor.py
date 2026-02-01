"""Resolve activation-time anchor date for plan scheduling."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pytz

from app.db import PlanDraftRecord
from app.time_slots import normalize_time_slot


def _normalize_timezone(name: str | None) -> pytz.BaseTzInfo:
    try:
        return pytz.timezone(name or "UTC")
    except pytz.UnknownTimeZoneError:
        return pytz.UTC


def _localize_datetime(
    *,
    target_date: datetime,
    target_time: time,
    tz: pytz.BaseTzInfo,
) -> datetime:
    naive = datetime.combine(target_date.date(), target_time)
    try:
        return tz.localize(naive)
    except pytz.NonExistentTimeError:
        return tz.localize(naive + timedelta(hours=1))
    except pytz.AmbiguousTimeError:
        return tz.localize(naive, is_dst=False)


def resolve_activation_anchor_date(
    *,
    draft: PlanDraftRecord,
    activation_time_utc: datetime,
    user_timezone: str | None,
    slot_time_mapping: dict[str, time],
) -> datetime:
    """
    Returns anchor_date (tz-aware, UTC) for plan scheduling.

    Rule:
    - If ANY day-1 slot is in the past at activation → anchor = tomorrow
    - Else → anchor = today
    """
    if activation_time_utc.tzinfo is None:
        raise ValueError("activation_time_not_timezone_aware")

    tz = _normalize_timezone(user_timezone)
    activation_time_utc = activation_time_utc.astimezone(timezone.utc)
    activation_local = activation_time_utc.astimezone(tz)

    day1_slots = [
        normalize_time_slot(step.time_slot)
        for step in draft.steps or []
        if step.day_number == 1
    ]

    should_shift = False
    for slot in day1_slots:
        slot_time = slot_time_mapping.get(slot)
        if not slot_time:
            raise ValueError("invalid_time_slot")
        slot_dt = _localize_datetime(
            target_date=activation_local,
            target_time=slot_time,
            tz=tz,
        )
        if slot_dt <= activation_local:
            should_shift = True
            break

    anchor_date = activation_local + timedelta(days=1 if should_shift else 0)
    anchor_local = _localize_datetime(
        target_date=anchor_date,
        target_time=time.min,
        tz=tz,
    )
    return anchor_local.astimezone(timezone.utc)
