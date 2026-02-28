from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

_BASE = Path(__file__).parent.parent / "resources"


def _load(filename: str) -> dict[str, Any]:
    with open(_BASE / filename, encoding="utf-8") as f:
        return json.load(f)


_TRIGGERS: dict[str, Any] | None = None
_PULSE: dict[str, Any] | None = None


def get_trigger_catalog() -> dict[str, Any]:
    global _TRIGGERS
    if _TRIGGERS is None:
        _TRIGGERS = _load("trigger_messages.json")
    return _TRIGGERS


def get_pulse_catalog() -> dict[str, Any]:
    global _PULSE
    if _PULSE is None:
        _PULSE = _load("pulse_quotes.json")
    return _PULSE


def get_trigger_message(trigger_id: str, persona: str, context: dict[str, Any]) -> str | None:
    milestone_triggers = {
        "streak_3",
        "streak_7",
        "comeback_after_skip",
        "first_task_ever",
        "day_all_done",
        "streak_broken",
    }

    if trigger_id == "task_completed":
        rationale = context.get("rationale")
        if rationale and random.random() < 0.4:
            name = context.get("name", "")
            prefix = f"{name}, " if name else ""
            return f"{prefix}{rationale}"

    catalog = get_trigger_catalog()
    trigger_data = catalog.get("triggers", {}).get(trigger_id, {})
    templates = trigger_data.get(persona) or trigger_data.get("empath")
    if not templates:
        return None
    return _fill(random.choice(templates), context)


def _fill(template: str, context: dict[str, Any]) -> str:
    try:
        return template.format_map({k: v for k, v in context.items() if v is not None})
    except (KeyError, ValueError):
        return template


def get_unused_quote(persona: str, used_indices: list[int]) -> tuple[str, str, str, int] | None:
    catalog = get_pulse_catalog()
    quotes = catalog.get("quotes", {}).get(persona, [])
    if not quotes:
        return None
    available = [i for i in range(len(quotes)) if i not in used_indices]
    if not available:
        available = list(range(len(quotes)))
    idx = random.choice(available)
    item = quotes[idx]
    return item["text"], item["author"], item.get("why", ""), idx


def get_coach_voice(persona: str) -> str | None:
    catalog = get_pulse_catalog()
    phrases = catalog.get("coach_voice", {}).get(persona, [])
    return random.choice(phrases) if phrases else None
