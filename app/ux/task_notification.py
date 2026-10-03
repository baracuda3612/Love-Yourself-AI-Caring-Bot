from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db import AIPlan, AIPlanDay
from app.content_library import selected_content, ContentValidationError
from html import escape

SLOT_EMOJI = {"MORNING": "🌅", "DAY": "☀️", "EVENING": "🌙"}
SLOT_LABEL = {"MORNING": "Ранок", "DAY": "День", "EVENING": "Вечір"}


def format_task_notification(db: Session, step, day, plan_day_number: int, task_index: int, task_total: int) -> str:
    if step.exercise_id:
        version = getattr(step, "content_version", None)
        if version is None:
            raise ContentValidationError("selected content version missing")
        display = selected_content(db, step.exercise_id, version)["display"]
    else:
        # Rows with no catalogue identity keep their original historical copy.
        display = {"title": step.title or "Завдання", "steps": [step.description or ""], "duration_label": ""}
    slot = (step.time_slot or "").upper()
    emoji = SLOT_EMOJI.get(slot, "🔔")
    label = SLOT_LABEL.get(slot, slot.capitalize() if slot else "День")
    lines = ["━━━━━━━━━━━━━━━━━━", f"{emoji} <b>{escape(display['title'])}</b>",
        f"День {plan_day_number} · {label} · {task_index} з {task_total}"]
    lines += ["", "📋 <b>Що робити:</b>", *[escape(text) for text in display["steps"]]]
    if display["duration_label"]:
        lines += ["", f"⏱ {escape(display['duration_label'])}"]
    lines.append("━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


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
