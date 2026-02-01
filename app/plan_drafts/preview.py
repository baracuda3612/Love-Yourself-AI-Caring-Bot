"""Backend preview builders for plan confirmation cards."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Tuple

from app.plan_parameters import normalize_plan_parameters

PreviewCard = Dict[str, Any]

_BUTTONS = [
    "✅ Confirm plan",
    "🔁 Regenerate",
    "✏️ Change parameters",
    "🔄 Restart from scratch",
]
# Buttons are UI transport hints.
# Backend must NOT branch logic based on button labels.


# NOTE:
# Preview layer is allowed to inspect draft structure
# for limited, non-interactive rendering only.
# This does NOT grant permission for business logic,
# task-level control, or plan interpretation.
def _extract_steps(draft_plan: Any) -> List[Dict[str, Any]]:
    if draft_plan is None:
        return []
    if isinstance(draft_plan, dict):
        steps = draft_plan.get("steps") or []
        return [
            {
                "day_number": step.get("day_number"),
                "exercise_name": step.get("exercise_name"),
                "category": step.get("category"),
                "time_slot": step.get("time_slot"),
            }
            for step in steps
            if isinstance(step, dict)
        ]
    if hasattr(draft_plan, "steps"):
        return [
            {
                "day_number": step.day_number,
                "exercise_name": step.exercise_name,
                "category": step.category,
                "time_slot": getattr(step.time_slot, "value", step.time_slot),
            }
            for step in list(draft_plan.steps or [])
        ]
    return []


def _select_preview_steps(steps: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ordered = sorted(
        steps,
        key=lambda step: (
            step.get("day_number") or 0,
            str(step.get("time_slot") or ""),
            str(step.get("exercise_name") or ""),
        ),
    )
    selected: List[Dict[str, Any]] = []
    seen: set[Tuple[Any, Any]] = set()
    for step in ordered:
        key = (step.get("day_number"), step.get("time_slot"))
        if key in seen:
            continue
        selected.append(step)
        seen.add(key)
        if len(selected) >= 5:
            break
    if len(selected) < 3:
        for step in ordered:
            if step in selected:
                continue
            selected.append(step)
            if len(selected) >= 3:
                break
    return selected


def build_confirmation_preview(
    draft_plan: Any, known_parameters: Dict[str, Any] | None
) -> PreviewCard:
    parameters = normalize_plan_parameters(known_parameters)
    steps = _select_preview_steps(_extract_steps(draft_plan))
    return {
        "header": "Попередній план (ще не активний)",
        "status": "DRAFT · NOT ACTIVE",
        "parameters": parameters,
        "steps": steps,
        "footer": "Цей план ще не активний.\nВи можете змінити параметри, перегенерувати план або підтвердити його.",
        "buttons": list(_BUTTONS),
    }


def render_confirmation_preview(card: PreviewCard) -> str:
    parameters = card.get("parameters") or {}
    preferred_slots = parameters.get("preferred_time_slots") or []
    slot_text = ", ".join(preferred_slots) if preferred_slots else "—"

    lines = [
        str(card.get("header") or ""),
        str(card.get("status") or ""),
        "",
        "Параметри плану",
        f"- Тривалість: {parameters.get('duration') or '—'}",
        f"- Фокус: {parameters.get('focus') or '—'}",
        f"- Навантаження: {parameters.get('load') or '—'}",
        f"- Часові слоти: {slot_text}",
        "",
        "Структура плану — приклад",
    ]

    for step in card.get("steps") or []:
        day_number = step.get("day_number") or "?"
        time_slot = str(step.get("time_slot") or "").capitalize() or "?"
        exercise_name = step.get("exercise_name") or "—"
        category = step.get("category") or "—"
        lines.append(f"Day {day_number} · {time_slot}")
        lines.append(f"– {exercise_name} ({category})")

    lines.extend(["", str(card.get("footer") or "")])
    return "\n".join(lines)


__all__ = [
    "PreviewCard",
    "build_confirmation_preview",
    "render_confirmation_preview",
]
