"""Shared helpers for plan parameter defaults."""

from __future__ import annotations

from typing import Any, Dict

PLAN_PARAMETER_DEFAULTS: Dict[str, Any] = {
    "duration": None,
    "focus": None,
    "load": None,
    "preferred_time_slots": None,
}


def normalize_plan_parameters(raw: Any) -> Dict[str, Any]:
    """Return a full plan parameter dict with defaults applied."""

    normalized = dict(PLAN_PARAMETER_DEFAULTS)
    if isinstance(raw, dict):
        for key in normalized:
            if key in raw:
                normalized[key] = raw[key]
    return normalized
