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
        "requires_params": False,
        "category": "LOAD_ADJUSTMENT",
        "affects_structure": True,
        "reversible": True,
    },
    AdaptationIntent.INCREASE_DAILY_LOAD: {
        "requires_params": False,
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
