"""Adjust plan draft steps based on activation time."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Iterable

import pytz

from app.db import PlanDraftStep
from app.time_slots import normalize_time_slot


@dataclass(frozen=True)
class AlignmentPatch:
    id: object
    day_number: int


def _normalize_timezone(name: str | None) -> pytz.BaseTzInfo:
    try:
        return pytz.timezone(name or "UTC")
    except pytz.UnknownTimeZoneError:
        return pytz.UTC


def _localize_slot_datetime(
    *,
    base_date: datetime,
    slot_time: time,
    tz: pytz.BaseTzInfo,
) -> datetime:
    naive = datetime.combine(base_date.date(), slot_time)
    try:
        return tz.localize(naive)
    except pytz.NonExistentTimeError:
        return tz.localize(naive + timedelta(hours=1))
    except pytz.AmbiguousTimeError:
        return tz.localize(naive, is_dst=False)


def _build_slot_time_mapping(
    slot_time_mapping: dict[str, time],
) -> dict[str, time]:
    normalized: dict[str, time] = {}
    for slot, slot_time in slot_time_mapping.items():
        normalized_slot = normalize_time_slot(slot)
        if not isinstance(slot_time, time):
            raise ValueError("slot_time_invalid")
        normalized[normalized_slot] = slot_time
    return normalized


def _unique_day_one_slots(steps: Iterable[PlanDraftStep]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for step in steps:
        if step.day_number != 1:
            continue
        slot = normalize_time_slot(step.time_slot)
        if slot in seen:
            continue
        seen.add(slot)
        ordered.append(slot)
    return ordered


def align_draft_steps_to_activation_time(
    *,
    draft_steps: list[PlanDraftStep],
    activation_time: datetime,  # tz-aware UTC
    timezone: str | None,
    slot_time_mapping: dict[str, time],
) -> dict[str, object]:
    """
    Returns alignment patches and start day offset based on activation time.
    """

    if activation_time.tzinfo is None:
        raise ValueError("activation_time_not_timezone_aware")
    activation_time = activation_time.astimezone(timezone.utc)
    if not draft_steps:
        return {"patches": [], "start_day_offset_days": 0}

    tz = _normalize_timezone(timezone)
    slot_times = _build_slot_time_mapping(slot_time_mapping)
    day_one_slots = _unique_day_one_slots(draft_steps)
    if not day_one_slots:
        return {"patches": [], "start_day_offset_days": 0}

    activation_local = activation_time.astimezone(tz)
    available_today: set[str] = set()
    missed_today: set[str] = set()
    for slot in day_one_slots:
        slot_time = slot_times.get(slot)
        if not slot_time:
            raise ValueError("slot_time_missing")
        slot_dt = _localize_slot_datetime(
            base_date=activation_local,
            slot_time=slot_time,
            tz=tz,
        )
        if slot_dt > activation_local:
            available_today.add(slot)
        else:
            missed_today.add(slot)

    start_day_offset_days = 1 if not available_today else 0
    if not available_today:
        return {"patches": [], "start_day_offset_days": 1}
    if not missed_today:
        return {"patches": [], "start_day_offset_days": start_day_offset_days}

    patches: list[AlignmentPatch] = []
    for step in draft_steps:
        new_day_number = step.day_number
        if step.day_number == 1:
            slot = normalize_time_slot(step.time_slot)
            if slot in missed_today:
                new_day_number = 2
        else:
            new_day_number = step.day_number + 1
        if new_day_number != step.day_number:
            patches.append(AlignmentPatch(id=step.id, day_number=new_day_number))
    return {"patches": patches, "start_day_offset_days": start_day_offset_days}
