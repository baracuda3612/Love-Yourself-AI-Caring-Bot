"""Plan-related UX messages."""

from __future__ import annotations

from typing import Iterable

_SLOT_ORDER = ("MORNING", "DAY", "EVENING")
_SLOT_TIMES = {
    "MORNING": "09:30",
    "DAY": "14:00",
    "EVENING": "21:00",
}


def build_activation_info_message(selected_slots: Iterable[str] | None, tz_name: str | None) -> str:
    """
    Returns follow-up info message with slot times, using fixed mapping:
    MORNING=09:30, DAY=14:00, EVENING=21:00.
    """
    del tz_name
    slots = [slot for slot in _SLOT_ORDER if slot in set(selected_slots or [])]
    times = [_SLOT_TIMES[slot] for slot in slots]
    times_text = ", ".join(times) if times else ", ".join(_SLOT_TIMES[slot] for slot in _SLOT_ORDER)
    return f"⏰ Завдання будуть о: {times_text}\nЧас можна змінити в меню."
