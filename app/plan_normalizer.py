"""Utilities for working with AI plan payloads."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List


_STEP_KEYS_PRIORITY: Iterable[str] = ("steps", "entries", "schedule")


def normalize_plan_steps(plan_payload: Any) -> List[Dict[str, Any]]:
    """Extract a list of step dictionaries from an AI plan payload.

    The planner may return steps in different keys (``steps``, ``entries``, or
    ``schedule``) and individual entries can contain redundant or unexpected
    fields.  This helper selects the preferred list and ensures every returned
    item contains at least a ``message`` field while preserving other fields.
    Invalid entries are skipped silently so downstream code can rely on the
    output being well-formed.
    """

    if not isinstance(plan_payload, dict):
        return []

    raw_steps: Iterable[Any] = []
    for key in _STEP_KEYS_PRIORITY:
        value = plan_payload.get(key)
        if isinstance(value, list):
            raw_steps = value
            break

    normalized: List[Dict[str, Any]] = []
    for item in raw_steps:
        if not isinstance(item, dict):
            continue

        message = item.get("message") or item.get("text")
        if not message:
            continue

        normalized_item: Dict[str, Any] = dict(item)
        normalized_item["message"] = message
        normalized_item["scheduled_for"] = item.get("scheduled_for") or item.get("time")

        normalized.append(normalized_item)

    return normalized

