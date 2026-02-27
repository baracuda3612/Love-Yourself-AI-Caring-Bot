from __future__ import annotations

from sqlalchemy.orm import Session

from app.db import ContentLibrary

SLOT_EMOJI = {"MORNING": "🌅", "DAY": "☀️", "EVENING": "🌙"}
SLOT_LABEL = {"MORNING": "Ранок", "DAY": "День", "EVENING": "Вечір"}


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
    rationale = payload.get("scientific_rationale", "")
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
    return content.content_payload.get("scientific_rationale") or None
