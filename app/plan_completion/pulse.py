from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from app.db import AIPlan, AIPlanDay, User
from app.plan_completion.pulse_phrases import PHRASES


@dataclass
class PulseDayEntry:
    day: int
    completion_ratio: float
    adapted: bool
    window: str
    is_today: bool


@dataclass
class PulseData:
    days: list[PulseDayEntry]
    plan_name: str
    plan_focus: str
    plan_total_days: int
    active_day_number: int
    plan_percent: int
    window_done: int
    window_total: int
    week_number: int
    persona: str
    phrase: str


def _to_ratio(completed: int, delivered: int) -> float:
    if delivered == 0:
        return 0.0
    valid = [i / delivered for i in range(delivered + 1)]
    raw = completed / delivered
    picked = min(valid, key=lambda v: abs(v - raw))
    return round(picked, 2)


def _is_step_completed(step: object) -> bool:
    status = getattr(step, "status", None)
    return bool(getattr(step, "is_completed", False) or status == "completed")


def _is_step_canceled(step: object) -> bool:
    return bool(getattr(step, "canceled_by_adaptation", False))


def _resolve_active_day_number(plan: AIPlan, days: list[AIPlanDay]) -> int:
    today_plan_day = getattr(plan, "current_day", None)
    if isinstance(today_plan_day, int) and today_plan_day > 0:
        return today_plan_day

    today = date.today()
    active_day_number = sum(
        1
        for d in days
        if getattr(d, "status", "active") != "paused"
        and (
            getattr(d, "date", None) is None
            or getattr(d, "date") <= today
        )
    )
    return max(active_day_number, 1)


def _resolve_window(plan_total_days: int, active_day_number: int) -> tuple[int, int]:
    if plan_total_days == 90:
        window_start = max(1, active_day_number - 13)
        window_end = min(plan_total_days, active_day_number + 6)
        return window_start, window_end

    week_index = (active_day_number - 1) // 7
    window_start = week_index * 7 + 1
    window_end = min(plan_total_days, window_start + 6)
    return window_start, window_end


def _resolve_persona(user: User) -> str:
    profile = getattr(user, "profile", None)
    if isinstance(profile, dict):
        return profile.get("persona", "empath")
    coach_persona = getattr(profile, "coach_persona", None) if profile else None
    return coach_persona or "empath"


def build_pulse_data(plan_id: int, db: Session) -> PulseData:
    plan = db.query(AIPlan).filter(AIPlan.id == plan_id).first()
    if not plan:
        raise ValueError("plan_not_found")

    user = db.query(User).filter(User.id == plan.user_id).first()
    if not user:
        raise ValueError("user_not_found")

    days = (
        db.query(AIPlanDay)
        .filter(AIPlanDay.plan_id == plan_id)
        .order_by(AIPlanDay.day_number)
        .all()
    )
    if not days:
        raise ValueError("days_not_found")

    plan_total_days = int(getattr(plan, "total_days", None) or len(days))
    active_day_number = _resolve_active_day_number(plan, days)
    active_day_number = min(active_day_number, plan_total_days)

    window_start, window_end = _resolve_window(plan_total_days, active_day_number)

    entries: list[PulseDayEntry] = []
    for d in days:
        steps = list(getattr(d, "steps", []) or [])
        delivered = sum(1 for s in steps if not _is_step_canceled(s))
        completed = sum(1 for s in steps if (not _is_step_canceled(s) and _is_step_completed(s)))
        completion_ratio = _to_ratio(completed, delivered)

        adapted = bool(
            getattr(d, "adapted", False)
            or getattr(d, "has_adaptation", False)
        )

        if d.day_number < window_start:
            window = "past"
        elif d.day_number <= window_end:
            window = "active"
        else:
            window = "future"

        entries.append(
            PulseDayEntry(
                day=d.day_number,
                completion_ratio=completion_ratio,
                adapted=adapted,
                window=window,
                is_today=(d.day_number == active_day_number),
            )
        )

    window_days = [entry for entry in entries if entry.window == "active"]
    window_done = sum(1 for entry in window_days if entry.completion_ratio > 0 or entry.adapted)
    window_total = len(window_days)

    persona = _resolve_persona(user)
    pool = PHRASES.get(persona, PHRASES["empath"])
    phrase_index = (plan_id + active_day_number) % len(pool)
    phrase = pool[phrase_index]

    return PulseData(
        days=entries,
        plan_name="",
        plan_focus="",
        plan_total_days=plan_total_days,
        active_day_number=active_day_number,
        plan_percent=round(active_day_number / plan_total_days * 100),
        window_done=window_done,
        window_total=window_total,
        week_number=math.ceil(active_day_number / 7),
        persona=persona,
        phrase=phrase,
    )
