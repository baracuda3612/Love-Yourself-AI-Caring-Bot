"""Deterministic Before/After preview renderer for ADAPTATION_CONFIRMATION.

No side effects. No DB access. No LLM calls.
Input:  intent string + params dict + active_plan dict.
Output: plain text string, Telegram-ready.
"""

from __future__ import annotations

from typing import Any, Dict, List

_LOAD_LABEL = {
    "LITE": "Легке (1/день)",
    "MID": "Середнє (2/день)",
    "INTENSIVE": "Інтенсивне (3/день)",
}
_SLOTS_TO_LOAD = {1: "LITE", 2: "MID", 3: "INTENSIVE"}
_LOAD_TO_SLOTS = {"LITE": 1, "MID": 2, "INTENSIVE": 3}
_CATEGORY_LABEL = {
    "somatic": "Тілесне",
    "cognitive": "Когнітивне",
    "boundaries": "Межі",
    "rest": "Відпочинок",
    "mixed": "Змішане",
}
_SLOT_LABEL = {"MORNING": "Ранок", "DAY": "День", "EVENING": "Вечір"}
_DIVIDER = "──────────────────"
_INTENT_SUCCESS_LABEL = {
    "REDUCE_DAILY_LOAD": "Навантаження зменшено",
    "INCREASE_DAILY_LOAD": "Навантаження збільшено",
    "PAUSE_PLAN": "План поставлено на паузу",
    "RESUME_PLAN": "План відновлено",
    "EXTEND_PLAN_DURATION": "Тривалість плану збільшено",
    "SHORTEN_PLAN_DURATION": "Тривалість плану скорочено",
    "CHANGE_MAIN_CATEGORY": "Категорію плану змінено",
}


def build_adaptation_preview(
    intent: str,
    params: Dict[str, Any] | None,
    active_plan: Dict[str, Any],
) -> str:
    """Build Telegram-ready Before/After preview for adaptation confirmation.

    Args:
        intent: AdaptationIntent value string (e.g. "REDUCE_DAILY_LOAD")
        params: adaptation params from session context (may be None or {})
        active_plan: dict from build_adaptation_payload ADAPTATION_CONFIRMATION block:
                     load, duration, focus, daily_task_count,
                     difficulty_level, status, current_day
    Returns:
        Formatted string. Never raises — returns safe fallback on any error.
    """
    try:
        return _render(intent, params or {}, active_plan)
    except Exception:
        return "🔄 Зміна плану\n\nПідтвердити зміни?"


def _render(intent: str, params: dict, plan: dict) -> str:
    load = plan.get("load") or "LITE"
    duration = plan.get("duration") or 0
    focus = (plan.get("focus") or "").lower()
    daily_count = plan.get("daily_task_count") or _LOAD_TO_SLOTS.get(load, 1)
    current_day = plan.get("current_day") or 1

    was: List[str] = []
    becomes: List[str] = []
    warning: str | None = None

    if intent == "REDUCE_DAILY_LOAD":
        slot_raw = (params.get("slot_to_remove") or "").upper()
        slot_label = _SLOT_LABEL.get(slot_raw, slot_raw.capitalize()) if slot_raw else "—"
        new_count = max(daily_count - 1, 1)
        new_load = _SLOTS_TO_LOAD.get(new_count, "LITE")
        was = [
            f"Навантаження: {_LOAD_LABEL.get(load, load)}",
            f"Слотів: {daily_count}/день",
            "Скасований слот: —",
        ]
        becomes = [
            f"Навантаження: {_LOAD_LABEL.get(new_load, new_load)}",
            f"Слотів: {new_count}/день",
            f"Скасований слот: {slot_label}",
        ]
        warning = "Завдання з цього слоту буде скасовано"

    elif intent == "INCREASE_DAILY_LOAD":
        slot_raw = (params.get("slot_to_add") or "").upper()
        slot_label = _SLOT_LABEL.get(slot_raw, slot_raw.capitalize()) if slot_raw else "—"
        new_count = min(daily_count + 1, 3)
        new_load = _SLOTS_TO_LOAD.get(new_count, "INTENSIVE")
        was = [
            f"Навантаження: {_LOAD_LABEL.get(load, load)}",
            f"Слотів: {daily_count}/день",
            "Новий слот: —",
        ]
        becomes = [
            f"Навантаження: {_LOAD_LABEL.get(new_load, new_load)}",
            f"Слотів: {new_count}/день",
            f"Новий слот: {slot_label}",
        ]

    elif intent == "PAUSE_PLAN":
        was = [
            "Статус: Активний",
            f"Прогрес: день {current_day} з {duration}",
            "Завдання: заплановано",
        ]
        becomes = [
            "Статус: На паузі",
            f"Прогрес: день {current_day} з {duration} ✓",
            "Завдання: скасовано до відновлення",
        ]
        warning = "Заплановані завдання буде скасовано"

    elif intent == "RESUME_PLAN":
        was = [
            "Статус: На паузі",
            f"Прогрес: день {current_day} з {duration}",
        ]
        becomes = [
            "Статус: Активний",
            f"Прогрес: день {current_day} з {duration} ✓",
        ]

    elif intent == "EXTEND_PLAN_DURATION":
        target = params.get("target_duration")
        if isinstance(target, int) and isinstance(duration, int) and target > duration:
            added = target - duration
            was = [
                f"Тривалість: {duration} днів",
                f"Останній день: {duration}",
            ]
            becomes = [
                f"Тривалість: {target} днів",
                f"Додається: {added} нових днів",
            ]
        else:
            was = [f"Тривалість: {duration} днів"]
            becomes = ["Тривалість: буде збільшено"]

    elif intent == "SHORTEN_PLAN_DURATION":
        target = params.get("target_duration")
        if isinstance(target, int) and isinstance(duration, int) and target < duration:
            removed = duration - target
            was = [
                f"Тривалість: {duration} днів",
                f"Останній день: {duration}",
            ]
            becomes = [
                f"Тривалість: {target} днів",
                f"Скасовується: {removed} днів (після дня {target})",
            ]
            warning = f"Завдання після дня {target} буде скасовано"
        else:
            was = [f"Тривалість: {duration} днів"]
            becomes = ["Тривалість: буде зменшено"]

    elif intent == "CHANGE_MAIN_CATEGORY":
        target_cat = (params.get("target_category") or "").lower()
        focus_label = _CATEGORY_LABEL.get(focus, focus.capitalize()) if focus else "—"
        target_label = _CATEGORY_LABEL.get(target_cat, target_cat.capitalize()) if target_cat else "—"
        was = [
            f"Категорія: {focus_label}",
            f"Прогрес: день {current_day} з {duration}",
            "Статус: Активний",
        ]
        becomes = [
            f"Категорія: {target_label}",
            "Прогрес: збережено ✓",
            "Новий план: день 1 (поточний на паузі)",
        ]
        warning = "Поточний план буде поставлено на паузу"

    else:
        was = ["Поточний стан"]
        becomes = ["Зміни буде застосовано"]

    return _format_card(was, becomes, warning)


def _format_card(
    was: List[str],
    becomes: List[str],
    warning: str | None,
) -> str:
    lines: List[str] = ["🔄 Зміна плану", _DIVIDER]

    lines.append("Було:")
    for item in was:
        lines.append(f"  {item}")

    lines.append("")
    lines.append("Стане:")
    for item in becomes:
        lines.append(f"  {item}")

    lines.append(_DIVIDER)

    if warning:
        lines.append(f"⚠️  {warning}")
        lines.append("")

    lines.append("Підтвердити?")
    return "\n".join(lines)


def build_adaptation_success_message(intent: str) -> str:
    """Build post-adaptation confirmation message shown after successful execution.

    Simple, deterministic. No plan data needed — just the intent.
    Never raises.
    """
    label = _INTENT_SUCCESS_LABEL.get(intent, "Зміни застосовано")
    if intent == "PAUSE_PLAN":
        status_line = "План поставлено на паузу."
    else:
        status_line = "План оновлено і вже активний."
    return f"✅ {label}.\n\n{status_line}"


__all__ = ["build_adaptation_preview", "build_adaptation_success_message"]
