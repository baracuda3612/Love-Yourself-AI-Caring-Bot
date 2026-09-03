from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db import AIPlan, AIPlanDay, ContentLibrary

SLOT_EMOJI = {"MORNING": "🌅", "DAY": "☀️", "EVENING": "🌙"}
SLOT_LABEL = {"MORNING": "Ранок", "DAY": "День", "EVENING": "Вечір"}


def _extract_rationale(payload: dict) -> str:
    """
    Single source of truth for reading scientific_rationale from content payload.
    Checks display sub-dict first (normalized format), then root level (legacy).
    """
    display = payload.get("display")
    if isinstance(display, dict):
        val = display.get("scientific_rationale")
        if val:
            return val
    return payload.get("scientific_rationale", "")


def format_task_notification(db: Session, step, day, plan_day_number: int, task_index: int, task_total: int) -> str:
    content = db.get(ContentLibrary, step.exercise_id) if step.exercise_id else None

    payload = {}
    title = step.title or "Завдання"
    if content and isinstance(content.content_payload, dict):
        payload = content.content_payload
        title = payload.get("title") or title

    slot = (step.time_slot or "").upper()
    emoji = SLOT_EMOJI.get(slot, "🔔")
    label = SLOT_LABEL.get(slot, slot.capitalize() if slot else "День")

    instructions = payload.get("instructions", "")
    rationale = _extract_rationale(payload)
    duration = payload.get("duration_estimate") or payload.get("duration_minutes")

    lines = [
        "━━━━━━━━━━━━━━━━━━",
        f"{emoji} <b>{title}</b>",
        f"День {plan_day_number} · {label} · {task_index} з {task_total}",
    ]
    if instructions:
        lines += ["", "📋 <b>Що робити:</b>", instructions]
    if rationale:
        lines += ["", "🧠 <b>Чому це працює:</b>", rationale]
    if duration:
        lines += ["", f"⏱ {duration}"]
    lines.append("━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def get_step_rationale(db: Session, step) -> str | None:
    if not step.exercise_id:
        return None
    content = db.get(ContentLibrary, step.exercise_id)
    if not content or not isinstance(content.content_payload, dict):
        return None
    val = _extract_rationale(content.content_payload)
    return val or None


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
