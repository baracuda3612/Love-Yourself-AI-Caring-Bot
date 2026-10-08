from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db import AIPlan, AIPlanDay
from app.exercise_presentation import step_presentation
from app.ux.exercise_renderer import render_exercise


def format_task_notification(db: Session, step, day, plan_day_number: int, task_index: int, task_total: int) -> str:
    # Retain the old callable signature for direct callers and persisted jobs.
    return render_exercise(step_presentation(db, step))


def get_step_rationale(db: Session, step) -> str | None:
    # The new exact-copy contract carries no outcome/rationale claims.
    return None


def _is_step_delivered(step) -> bool:
    if getattr(step, "is_delivered", False):
        return True
    if getattr(step, "delivered_at", None) is not None:
        return True

    scheduled_for = getattr(step, "scheduled_for", None)
    if scheduled_for is None:
        return False

    now_utc = datetime.now(timezone.utc)
    if getattr(scheduled_for, "tzinfo", None) is None:
        scheduled_for = scheduled_for.replace(tzinfo=timezone.utc)
    return scheduled_for <= now_utc


def maybe_advance_current_day(db: Session, plan_id: int, day_number: int) -> bool:
    """Compatibility API returning whether calculated progress passed a day.

    WP-01.3 no longer writes the legacy ``ai_plans.current_day`` mirror.
    """
    from app.lifecycle import derive_current_day

    plan = db.query(AIPlan).filter(AIPlan.id == plan_id).first()
    if not plan:
        return False
    total_days = int(getattr(plan, "total_days", 0) or 0)
    return bool(total_days and derive_current_day(db, plan_id, total_days) > day_number)
