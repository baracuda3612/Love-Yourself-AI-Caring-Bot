from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import pytz
from sqlalchemy.orm import Session, joinedload

from app.db import AIPlan, AIPlanDay, UserEvent

DayStatus = Literal["done", "partial", "skipped", "ignored", "future"]


@dataclass
class DayEntry:
    day: int
    status: DayStatus


def get_plan_timeline(db: Session, user_id: int, plan_id: int) -> list[DayEntry]:
    plan = db.query(AIPlan).filter(AIPlan.id == plan_id, AIPlan.user_id == user_id).first()
    if not plan:
        return []

    days = (
        db.query(AIPlanDay)
        .filter(AIPlanDay.plan_id == plan_id)
        .options(joinedload(AIPlanDay.steps))
        .order_by(AIPlanDay.day_number)
        .all()
    )

    plan_step_ids = [
        step.id for day in days for step in day.steps if not step.canceled_by_adaptation
    ]

    if not plan_step_ids:
        return []

    completed_ids: set[int] = set()
    skipped_ids: set[int] = set()

    for ev in (
        db.query(UserEvent)
        .filter(
            UserEvent.user_id == user_id,
            UserEvent.event_type.in_(["task_completed", "task_skipped", "task_ignored"]),
            UserEvent.step_id.in_([str(i) for i in plan_step_ids]),
        )
        .all()
    ):
        sid = int(ev.step_id)
        if ev.event_type == "task_completed":
            completed_ids.add(sid)
        elif ev.event_type == "task_skipped":
            skipped_ids.add(sid)

    now = datetime.now(pytz.UTC)
    result: list[DayEntry] = []

    for day in days:
        active = [s for s in day.steps if not s.canceled_by_adaptation]
        if not active:
            continue
        ids = {s.id for s in active}

        if all(s.scheduled_for and s.scheduled_for.replace(tzinfo=pytz.UTC) > now for s in active):
            result.append(DayEntry(day=day.day_number, status="future"))
            continue

        n_done = len(ids & completed_ids)
        n_skipped = len(ids & skipped_ids)
        n_total = len(active)

        if n_done == n_total:
            status: DayStatus = "done"
        elif n_skipped == n_total:
            status = "skipped"
        elif n_done > 0:
            status = "partial"
        else:
            status = "ignored"

        result.append(DayEntry(day=day.day_number, status=status))

    return result
