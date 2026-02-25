"""Adaptation types domain model.

This module is the single source of truth for all adaptation types.
Used by: LLM tool schemas, backend executors, eligibility checks.

CRITICAL: All adaptation properties are derived from ADAPTATION_METADATA.
Do not create separate sets/constants that duplicate metadata fields.
"""

from __future__ import annotations

from enum import Enum
from typing import TypedDict, Set


class AdaptationMeta(TypedDict):
    """Metadata for one adaptation intent."""

    requires_params: bool
    category: str
    affects_structure: bool
    reversible: bool


class AdaptationIntent(str, Enum):
    """All supported adaptation types.

    Each value represents a distinct adaptation that can be applied to an active plan.
    These values are used across LLM tool schemas, backend executors,
    eligibility checks, and telemetry.
    """

    REDUCE_DAILY_LOAD = "REDUCE_DAILY_LOAD"
    INCREASE_DAILY_LOAD = "INCREASE_DAILY_LOAD"
    LOWER_DIFFICULTY = "LOWER_DIFFICULTY"
    INCREASE_DIFFICULTY = "INCREASE_DIFFICULTY"
    EXTEND_PLAN_DURATION = "EXTEND_PLAN_DURATION"
    SHORTEN_PLAN_DURATION = "SHORTEN_PLAN_DURATION"
    PAUSE_PLAN = "PAUSE_PLAN"
    RESUME_PLAN = "RESUME_PLAN"
    CHANGE_MAIN_CATEGORY = "CHANGE_MAIN_CATEGORY"


# SINGLE SOURCE OF TRUTH: All properties derived from this metadata.
ADAPTATION_METADATA: dict[AdaptationIntent, AdaptationMeta] = {
    AdaptationIntent.REDUCE_DAILY_LOAD: {
        "requires_params": True,
        "category": "LOAD_ADJUSTMENT",
        "affects_structure": True,
        "reversible": True,
    },
    AdaptationIntent.INCREASE_DAILY_LOAD: {
        "requires_params": True,
        "category": "LOAD_ADJUSTMENT",
        "affects_structure": True,
        "reversible": True,
    },
    AdaptationIntent.LOWER_DIFFICULTY: {
        "requires_params": False,
        "category": "DIFFICULTY_ADJUSTMENT",
        "affects_structure": True,
        "reversible": True,
    },
    AdaptationIntent.INCREASE_DIFFICULTY: {
        "requires_params": False,
        "category": "DIFFICULTY_ADJUSTMENT",
        "affects_structure": True,
        "reversible": True,
    },
    AdaptationIntent.EXTEND_PLAN_DURATION: {
        "requires_params": True,
        "category": "DURATION_ADJUSTMENT",
        "affects_structure": True,
        "reversible": False,
    },
    AdaptationIntent.SHORTEN_PLAN_DURATION: {
        "requires_params": True,
        "category": "DURATION_ADJUSTMENT",
        "affects_structure": True,
        "reversible": False,
    },
    AdaptationIntent.PAUSE_PLAN: {
        "requires_params": False,
        "category": "EXECUTION_STATE",
        "affects_structure": False,
        "reversible": True,
    },
    AdaptationIntent.RESUME_PLAN: {
        "requires_params": False,
        "category": "EXECUTION_STATE",
        "affects_structure": False,
        "reversible": True,
    },
    AdaptationIntent.CHANGE_MAIN_CATEGORY: {
        "requires_params": True,
        "category": "FOCUS_CHANGE",
        "affects_structure": True,
        "reversible": False,
    },
}


def get_intents_by_category(category: str) -> Set[AdaptationIntent]:
    """Return all intents for a given metadata category."""
    return {
        intent
        for intent, meta in ADAPTATION_METADATA.items()
        if meta["category"] == category
    }


LOAD_ADJUSTMENTS: Set[AdaptationIntent] = get_intents_by_category("LOAD_ADJUSTMENT")
DIFFICULTY_ADJUSTMENTS: Set[AdaptationIntent] = get_intents_by_category("DIFFICULTY_ADJUSTMENT")
DURATION_ADJUSTMENTS: Set[AdaptationIntent] = get_intents_by_category("DURATION_ADJUSTMENT")
EXECUTION_STATE_CHANGES: Set[AdaptationIntent] = get_intents_by_category("EXECUTION_STATE")


def requires_params(intent: AdaptationIntent) -> bool:
    """Check if adaptation requires parameter collection."""
    return ADAPTATION_METADATA[intent]["requires_params"]


def get_adaptation_category(intent: AdaptationIntent) -> str:
    """Get adaptation category for grouping and analytics."""
    return ADAPTATION_METADATA[intent]["category"]


def is_structural(intent: AdaptationIntent) -> bool:
    """Check if adaptation modifies plan structure."""
    return ADAPTATION_METADATA[intent]["affects_structure"]


def is_reversible(intent: AdaptationIntent) -> bool:
    """Check if adaptation can be undone."""
    return ADAPTATION_METADATA[intent]["reversible"]


def get_all_intent_values() -> list[str]:
    """Get all enum values as strings."""
    return [intent.value for intent in AdaptationIntent]


def get_intents_requiring_params() -> list[str]:
    """Get intent values that require parameter collection."""
    return [
        intent.value
        for intent, meta in ADAPTATION_METADATA.items()
        if meta["requires_params"]
    ]


def get_structural_intents() -> Set[AdaptationIntent]:
    """Get intents that modify plan structure."""
    return {
        intent
        for intent, meta in ADAPTATION_METADATA.items()
        if meta["affects_structure"]
    }


def get_non_structural_intents() -> Set[AdaptationIntent]:
    """Get intents that only change execution state."""
    return {
        intent
        for intent, meta in ADAPTATION_METADATA.items()
        if not meta["affects_structure"]
    }


# ── Conflict Matrix ──────────────────────────────────────────────────────────
# Maps intent → set of intents that CANNOT be applied immediately before it.
# Check: if last_applied_intent in ADAPTATION_CONFLICT_MATRIX[current_intent] → block.
ADAPTATION_CONFLICT_MATRIX: dict[AdaptationIntent, set[AdaptationIntent]] = {
    AdaptationIntent.REDUCE_DAILY_LOAD: {
        AdaptationIntent.REDUCE_DAILY_LOAD,
    },
    AdaptationIntent.INCREASE_DAILY_LOAD: {
        AdaptationIntent.INCREASE_DAILY_LOAD,
    },
    AdaptationIntent.PAUSE_PLAN: {
        AdaptationIntent.PAUSE_PLAN,
    },
    AdaptationIntent.RESUME_PLAN: {
        AdaptationIntent.RESUME_PLAN,
    },
    AdaptationIntent.CHANGE_MAIN_CATEGORY: {
        AdaptationIntent.CHANGE_MAIN_CATEGORY,
        AdaptationIntent.PAUSE_PLAN,
    },
    AdaptationIntent.EXTEND_PLAN_DURATION: {
        AdaptationIntent.EXTEND_PLAN_DURATION,
        AdaptationIntent.SHORTEN_PLAN_DURATION,
    },
    AdaptationIntent.SHORTEN_PLAN_DURATION: {
        AdaptationIntent.SHORTEN_PLAN_DURATION,
        AdaptationIntent.EXTEND_PLAN_DURATION,
    },
}


def check_adaptation_conflict(
    intent: AdaptationIntent,
    last_applied_intent: AdaptationIntent | None,
    current_plan_load: str | None,
    current_plan_status: str | None,
) -> str | None:
    """
    Returns a human-readable reason string if adaptation is blocked.
    Returns None if allowed.

    Call this BEFORE entering ADAPTATION_CONFIRMATION state.
    Do NOT call inside executor.
    """
    if intent == AdaptationIntent.REDUCE_DAILY_LOAD and current_plan_load == "LITE":
        return "already_minimum_load"
    if intent == AdaptationIntent.INCREASE_DAILY_LOAD and current_plan_load == "INTENSIVE":
        return "already_maximum_load"
    if intent == AdaptationIntent.PAUSE_PLAN and current_plan_status == "paused":
        return "plan_already_paused"
    if intent == AdaptationIntent.RESUME_PLAN and current_plan_status == "active":
        return "plan_not_paused"

    if last_applied_intent is not None:
        blocked_after = ADAPTATION_CONFLICT_MATRIX.get(intent, set())
        if last_applied_intent in blocked_after:
            return f"conflicts_with_previous_{last_applied_intent.value}"

    return None


# ── Rate Limits ──────────────────────────────────────────────────────────────
ADAPTATION_RATE_LIMITS: dict[str, dict] = {
    "LOAD_ADJUSTMENT": {
        "max_per_day": 2,
        "max_total": 10,
        "cooldown_minutes": 30,
    },
    "DIFFICULTY_ADJUSTMENT": {
        "max_per_day": 2,
        "max_total": 10,
        "cooldown_minutes": 30,
    },
    "DURATION_ADJUSTMENT": {
        "max_per_day": 1,
        "max_total": 3,
        "cooldown_minutes": 60,
    },
    "EXECUTION_STATE": {
        "max_per_day": 5,
        "max_total": None,
        "cooldown_minutes": 0,
    },
    "FOCUS_CHANGE": {
        "max_per_day": 1,
        "max_total": 2,
        "cooldown_minutes": 120,
    },
}


def check_rate_limit(
    intent: AdaptationIntent,
    history_entries: list,
    now_utc,
) -> str | None:
    """
    Returns reason string if rate limited, else None.
    history_entries: already filtered to this plan, ordered by applied_at DESC.
    """
    category = get_adaptation_category(intent)
    limits = ADAPTATION_RATE_LIMITS.get(category)
    if not limits:
        return None

    category_entries = [e for e in history_entries if e.category == category and not e.is_rolled_back]

    cooldown_minutes = limits.get("cooldown_minutes", 0)
    if cooldown_minutes > 0 and category_entries:
        last = category_entries[0]
        elapsed = (now_utc - last.applied_at).total_seconds() / 60
        if elapsed < cooldown_minutes:
            remaining = int(cooldown_minutes - elapsed)
            return f"cooldown_active_{remaining}_minutes"

    max_per_day = limits.get("max_per_day")
    if max_per_day is not None:
        # TODO: day_start is computed in UTC. For users in non-UTC timezones this means
        # the "day" boundary may differ from their local midnight by up to ±12h.
        # Fix: pass user.timezone and localize day_start accordingly.
        # Acceptable for MVP — affects edge cases only (e.g. user in UTC+2 at 23:00 local).
        day_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        today_count = sum(1 for e in category_entries if e.applied_at >= day_start)
        if today_count >= max_per_day:
            return f"daily_limit_reached_{today_count}_of_{max_per_day}"

    max_total = limits.get("max_total")
    if max_total is not None and len(category_entries) >= max_total:
        return f"total_limit_reached_{len(category_entries)}_of_{max_total}"

    return None


# ── Inverse Intent Map ────────────────────────────────────────────────────────
# Semantic undo policy:
# - LOAD / DIFFICULTY / EXECUTION_STATE: inverse intent is applied as a NEW adaptation.
#   This means exercises are NOT restored to their exact pre-adaptation state.
#   UX expectation: "undo load change" = new symmetrical adaptation, not exact revert.
# - DURATION / FOCUS (None values below): no automatic undo.
#   Reason: CHANGE_MAIN_CATEGORY creates a new plan — old plan is already paused (IS the rollback).
#   EXTEND/SHORTEN: structural changes to step history make safe restore ambiguous.
INVERSE_INTENT: dict[AdaptationIntent, AdaptationIntent | None] = {
    AdaptationIntent.REDUCE_DAILY_LOAD: AdaptationIntent.INCREASE_DAILY_LOAD,
    AdaptationIntent.INCREASE_DAILY_LOAD: AdaptationIntent.REDUCE_DAILY_LOAD,
    AdaptationIntent.LOWER_DIFFICULTY: AdaptationIntent.INCREASE_DIFFICULTY,
    AdaptationIntent.INCREASE_DIFFICULTY: AdaptationIntent.LOWER_DIFFICULTY,
    AdaptationIntent.PAUSE_PLAN: AdaptationIntent.RESUME_PLAN,
    AdaptationIntent.RESUME_PLAN: AdaptationIntent.PAUSE_PLAN,
    AdaptationIntent.EXTEND_PLAN_DURATION: None,
    AdaptationIntent.SHORTEN_PLAN_DURATION: None,
    AdaptationIntent.CHANGE_MAIN_CATEGORY: None,
}


def get_inverse_intent(intent: AdaptationIntent) -> AdaptationIntent | None:
    return INVERSE_INTENT.get(intent)


class AdaptationNotEligibleError(ValueError):
    """Raised when adaptation cannot be applied due to current plan state."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason
