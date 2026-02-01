"""
Plan composition rules from PLAN COMPOSITION RULES & LOGIC MATRIX.
Pure functions - no side effects, no LLM calls.
"""

import hashlib
from typing import Dict, List

from app.plan_drafts.plan_types import Duration, Exercise, Focus, Load, PlanParameters, SlotType, TimeSlot


# Rule 3: FOCUS TYPOLOGY & CONSISTENCY (80/20 Rule)
FOCUS_DISTRIBUTION = {
    Focus.SOMATIC: {
        "dominant": 0.8,  # 80% somatic
        "complementary": {
            Focus.COGNITIVE: 0.1,
            Focus.REST: 0.1,
        },
    },
    Focus.COGNITIVE: {
        "dominant": 0.8,
        "complementary": {
            Focus.SOMATIC: 0.1,
            Focus.BOUNDARIES: 0.1,
        },
    },
    Focus.BOUNDARIES: {
        "dominant": 0.8,
        "complementary": {
            Focus.COGNITIVE: 0.15,
            Focus.REST: 0.05,
        },
    },
    Focus.REST: {
        "dominant": 0.9,  # Rest is more pure
        "complementary": {
            Focus.SOMATIC: 0.1,
        },
    },
    Focus.MIXED: {
        "dominant": 0.4,  # More balanced
        "complementary": {
            Focus.SOMATIC: 0.25,
            Focus.COGNITIVE: 0.25,
            Focus.BOUNDARIES: 0.1,
        },
    },
}


# Rule 6: LOAD MATRIX (Slot Allocation)
LOAD_SLOTS = {
    Load.LITE: {"total": 1, "structure": [SlotType.CORE]},
    Load.MID: {"total": 2, "structure": [SlotType.CORE, SlotType.SUPPORT]},
    Load.INTENSIVE: {
        "total": 3,
        "structure": [SlotType.CORE, SlotType.SUPPORT, SlotType.REST],
    },
}


# Rule 11: TIME SLOT CONTRACT - Slot type to time mapping
SLOT_TIME_PREFERENCES = {
    SlotType.CORE: [TimeSlot.MORNING, TimeSlot.DAY],
    SlotType.SUPPORT: [TimeSlot.DAY, TimeSlot.EVENING],
    SlotType.EMERGENCY: [TimeSlot.EVENING],
    SlotType.REST: [TimeSlot.EVENING],
}


# Rule 7: DURATION DYNAMICS
DURATION_CONFIG = {
    Duration.SHORT: {
        "min_days": 7,
        "max_days": 14,
        "default_days": 10,
        "intensity_curve": "flat",  # Constant intensity
        "description": "Sprint - rapid stabilization",
    },
    Duration.STANDARD: {
        "min_days": 21,
        "max_days": 21,
        "default_days": 21,
        "intensity_curve": "progressive",  # Increase after week 1
        "description": "Habit - stable rhythm with progression",
    },
    Duration.LONG: {
        "min_days": 90,
        "max_days": 90,
        "default_days": 90,
        "intensity_curve": "wave",  # Active/maintenance alternation
        "description": "Transformation - wave-like pacing",
    },
}


def get_total_days(duration: Duration) -> int:
    """Get number of days for a duration type"""

    return DURATION_CONFIG[duration]["default_days"]


def get_daily_slot_structure(load: Load) -> List[SlotType]:
    """
    Rule 6: Get slot structure for a given load mode.
    Returns list of slot types that should be filled each day.
    """

    return LOAD_SLOTS[load]["structure"].copy()


def get_time_slot_for_slot_type(
    slot_type: SlotType, user_preferences: List[str] | None = None
) -> TimeSlot:
    """
    Rule 11: Assign time slot based on slot type.
    Respects user preferences if provided.
    """

    preferred_times = SLOT_TIME_PREFERENCES.get(slot_type, [TimeSlot.DAY])
    if user_preferences:
        normalized_preferences = [
            slot for slot in user_preferences if slot in {time_slot.value for time_slot in TimeSlot}
        ]
        if normalized_preferences:
            for preferred in normalized_preferences:
                for slot in preferred_times:
                    if slot.value == preferred:
                        return slot
            return TimeSlot(normalized_preferences[0])
    return preferred_times[0]


def calculate_category_distribution(focus: Focus, total_slots: int) -> Dict[str, int]:
    """
    Rule 3: Calculate how many slots each category should get.
    Applies 80/20 rule (or appropriate distribution for focus type).

    Returns: dict mapping category to number of slots
    """

    distribution = FOCUS_DISTRIBUTION[focus]

    result: Dict[str, int] = {}

    dominant_count = int(total_slots * distribution["dominant"])
    result[focus.value] = dominant_count

    remaining = total_slots - dominant_count
    complementary = distribution["complementary"]

    for cat, ratio in complementary.items():
        count = int(remaining * ratio)
        result[cat.value] = result.get(cat.value, 0) + count

    total_assigned = sum(result.values())
    if total_assigned < total_slots:
        result[focus.value] += total_slots - total_assigned

    if total_slots > 1 and result.get(focus.value, 0) == total_slots:
        for cat in complementary.keys():
            if result.get(cat.value, 0) == 0:
                result[cat.value] = 1
                result[focus.value] -= 1
                break

    return result


def should_use_exercise(exercise: Exercise, params: PlanParameters) -> bool:
    """
    Rule 1, 4, 5: Check if exercise can be used.

    Checks:
    - Is exercise active?
    - Not in cooldown?
    - Allowed by user policy?
    """

    if not exercise.is_active:
        return False

    if params.user_policy:
        policy = params.user_policy

        if not policy.allows_category(exercise.category):
            return False

        if not policy.allows_impact_area(exercise.impact_areas):
            return False

    return True


def get_difficulty_for_week(week_number: int, duration: Duration) -> int:
    """
    Rule 7: Calculate appropriate difficulty based on week and duration.

    SHORT: Constant difficulty (1-2)
    STANDARD: Progressive - start easier, increase after week 1
    LONG: Wave pattern - alternate between challenging and maintenance
    """

    intensity_curve = DURATION_CONFIG[duration]["intensity_curve"]

    if intensity_curve == "flat":
        return 1 if week_number == 1 else 2

    if intensity_curve == "progressive":
        if week_number == 1:
            return 1
        if week_number == 2:
            return 2
        return 3

    if intensity_curve == "wave":
        week_in_cycle = ((week_number - 1) % 5) + 1

        if week_in_cycle in [1, 2]:
            return 1 if week_in_cycle == 1 else 2
        if week_in_cycle in [3, 4]:
            return 2 if week_in_cycle == 3 else 3
        return 1

    return 2


def filter_exercises_by_criteria(
    exercises: List[Exercise],
    category: str | None = None,
    priority_tier: SlotType | None = None,
    max_difficulty: int | None = None,
    impact_areas: List[str] | None = None,
) -> List[Exercise]:
    """
    Filter exercises by various criteria.
    Used for smart fallback (Rule 5) and general selection.
    """

    filtered = exercises

    if category:
        filtered = [e for e in filtered if e.category == category]

    if priority_tier:
        tier_value = priority_tier.value
        filtered = [e for e in filtered if e.priority_tier == tier_value]

    if max_difficulty:
        filtered = [e for e in filtered if e.difficulty <= max_difficulty]

    if impact_areas:
        filtered = [e for e in filtered if any(ia in e.impact_areas for ia in impact_areas)]

    return filtered


def select_exercise_with_fallback(
    exercises: List[Exercise],
    preferred_category: str,
    slot_type: SlotType,
    max_difficulty: int,
    params: PlanParameters,
    seed_suffix: str = "",
) -> Exercise | None:
    """
    Rule 5: IMPACT AREA MATCHING (Smart Fallback)

    Try to find exercise in preferred category.
    If blocked, fallback to ANY category with matching impact areas.
    """

    available = [e for e in exercises if should_use_exercise(e, params)]

    if not available:
        return None

    preferred = filter_exercises_by_criteria(
        available,
        category=preferred_category,
        priority_tier=slot_type,
        max_difficulty=max_difficulty,
    )

    if preferred:
        return _deterministic_choice(preferred, seed_suffix=seed_suffix)

    category_exercises = filter_exercises_by_criteria(exercises, category=preferred_category)

    if category_exercises:
        common_impacts = set()
        for exercise in category_exercises[:5]:
            common_impacts.update(exercise.impact_areas)

        fallback = filter_exercises_by_criteria(
            available,
            priority_tier=slot_type,
            max_difficulty=max_difficulty,
            impact_areas=list(common_impacts),
        )

        if fallback:
            return _deterministic_choice(fallback, seed_suffix=seed_suffix)

    last_resort = filter_exercises_by_criteria(
        available,
        priority_tier=slot_type,
        max_difficulty=max_difficulty,
    )

    return _deterministic_choice(last_resort, seed_suffix=seed_suffix) if last_resort else None


def _deterministic_choice(exercises: List[Exercise], seed_suffix: str = "") -> Exercise | None:
    """
    Deterministic selection by base_weight, then internal_name, then id.
    """

    if not exercises:
        return None

    if seed_suffix:
        def _seeded_hash(exercise: Exercise) -> str:
            raw = f"{seed_suffix}:{exercise.id}".encode("utf-8")
            return hashlib.sha256(raw).hexdigest()

        return sorted(
            exercises,
            key=lambda e: (-float(e.base_weight), _seeded_hash(e), str(e.internal_name), str(e.id)),
        )[0]

    return sorted(
        exercises,
        key=lambda e: (-float(e.base_weight), str(e.internal_name), str(e.id)),
    )[0]
