"""
Draft Plan Builder - deterministic plan composition.

This module takes PlanParameters and ContentLibrary,
applies all composition rules, and produces a PlanDraft artifact.

NO LLM calls here - pure algorithmic composition.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List

from app.plan_drafts.plan_types import (
    Duration,
    Exercise,
    Focus,
    Load,
    PlanDraft,
    PlanParameters,
    PlanStep,
    SlotType,
    TimeSlot,
)
from app.plan_drafts.rules import (
    calculate_category_distribution,
    get_daily_slot_structure,
    get_difficulty_for_week,
    get_time_slot_for_slot_type,
    get_total_days,
    select_exercise_with_fallback,
)
from app.plan_drafts.validators import validate_plan_draft, validate_three_pillars


class DraftValidationError(Exception):
    def __init__(self, errors: List[str]):
        super().__init__("Draft validation failed")
        self.errors = errors


class InsufficientLibraryError(Exception):
    """Content library too small for requested plan"""


EXPECTED_SLOTS_PER_DAY = {
    Load.LITE: 1,
    Load.MID: 2,
    Load.INTENSIVE: 3,
}

@dataclass
class Blueprint:
    id: str
    target_module: str
    duration_variants: dict


class ContentLibrary:
    """Wrapper for content library JSON"""

    def __init__(self, library_path: str):
        with open(library_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.exercises: List[Exercise] = [
            Exercise.from_library_item(item) for item in data.get("inventory", [])
        ]
        self.blueprints: List[Blueprint] = [
            Blueprint(
                id=bp["id"],
                target_module=bp.get("target_module", ""),
                duration_variants=bp.get("duration_variants", {}),
            )
            for bp in data.get("blueprints", [])
        ]

    def get_active_exercises(self) -> List[Exercise]:
        """Get only active exercises"""

        return [e for e in self.exercises if e.is_active]

    def get_exercise_by_id(self, exercise_id: str) -> Exercise | None:
        """Find exercise by ID"""

        for exercise in self.exercises:
            if exercise.id == exercise_id:
                return exercise
        return None


class DraftBuilder:
    """
    Main plan composition engine.
    Applies all rules from PLAN COMPOSITION RULES & LOGIC MATRIX.
    """

    def __init__(self, content_library: ContentLibrary, user_id: str = ""):
        self.library = content_library
        self.exercise_last_used: Dict[str, int] = {}
        self.user_id = user_id

    def _is_in_cooldown(self, exercise_id: str, current_day: int, cooldown_days: int) -> bool:
        """
        Check if exercise is in cooldown period.
        Rule 4: cooldown_days means minimum days between uses.
        cooldown_days=1 means "can't use on consecutive days" (need at least 1 day gap)
        """

        if exercise_id not in self.exercise_last_used:
            return False

        last_used = self.exercise_last_used[exercise_id]
        days_since = current_day - last_used

        return days_since <= cooldown_days

    def build_plan_draft(self, params: PlanParameters) -> PlanDraft:
        """
        Main entry point.

        Rule 2: Requires all Three Pillars to be defined.
        Returns: PlanDraft with validation errors if any.
        """

        self.exercise_last_used = {}
        pillar_errors = validate_three_pillars(params)
        if pillar_errors:
            raise DraftValidationError(pillar_errors)

        expected_slots = EXPECTED_SLOTS_PER_DAY[params.load]
        if not params.user_policy or not params.user_policy.preferred_time_slots:
            raise DraftValidationError(["preferred_time_slots required"])
        if len(params.user_policy.preferred_time_slots) != expected_slots:
            raise DraftValidationError(
                [
                    f"Expected {expected_slots} time slots for load {params.load.value}, "
                    f"got {len(params.user_policy.preferred_time_slots)}"
                ]
            )

        total_days = get_total_days(params.duration)

        slots_per_day = len(get_daily_slot_structure(params.load))
        total_slots = total_days * slots_per_day
        category_distribution = calculate_category_distribution(params.focus, total_slots)

        steps: List[PlanStep] = []
        available_exercises = self.library.get_active_exercises()
        if not available_exercises:
            raise InsufficientLibraryError("Content library has no active exercises")

        for day in range(1, total_days + 1):
            week_number = ((day - 1) // 7) + 1
            max_difficulty = get_difficulty_for_week(week_number, params.duration)

            slot_structure = get_daily_slot_structure(params.load)
            expected_count = EXPECTED_SLOTS_PER_DAY[params.load]
            assert len(slot_structure) == expected_count

            used_slots_today: List[TimeSlot] = []

            for slot_index, slot_type in enumerate(slot_structure):
                category = self._pick_category_for_slot(category_distribution, params.focus)

                available_exercises_now = [
                    e
                    for e in available_exercises
                    if not self._is_in_cooldown(e.id, day, e.cooldown_days)
                ]

                seed_key = f"{self.user_id}:{day}:{slot_index}"
                exercise = select_exercise_with_fallback(
                    available_exercises_now,
                    preferred_category=category,
                    slot_type=slot_type,
                    max_difficulty=max_difficulty,
                    params=params,
                    seed_key=seed_key,
                )

                if not exercise:
                    fallback_candidates = [
                        e
                        for e in available_exercises
                        if not self._is_in_cooldown(e.id, day, e.cooldown_days)
                    ]
                    exercise = select_exercise_with_fallback(
                        fallback_candidates,
                        preferred_category=category,
                        slot_type=slot_type,
                        max_difficulty=max_difficulty,
                        params=params,
                        seed_key=seed_key,
                    )

                if not exercise:
                    raise InsufficientLibraryError(
                        f"No exercise found for day {day}, slot {slot_type.value}"
                    )

                time_slot = get_time_slot_for_slot_type(
                    slot_type,
                    params.user_policy.preferred_time_slots if params.user_policy else None,
                    already_used_slots=used_slots_today,
                )
                used_slots_today.append(time_slot)

                step = PlanStep(
                    step_id=f"step_{day}_{slot_index}",
                    day_number=day,
                    exercise_id=exercise.id,
                    exercise_name=exercise.internal_name,
                    category=exercise.category,
                    impact_areas=exercise.impact_areas,
                    slot_type=slot_type,
                    time_slot=time_slot,
                    difficulty=exercise.difficulty,
                    energy_cost=exercise.energy_cost,
                )

                steps.append(step)

                if category in category_distribution:
                    category_distribution[category] -= 1

                self.exercise_last_used[exercise.id] = day

        if len(steps) != total_slots:
            raise InsufficientLibraryError(
                f"Expected {total_slots} steps but generated {len(steps)}"
            )

        draft = PlanDraft(
            id=str(uuid.uuid4()),
            duration=params.duration,
            focus=params.focus,
            load=params.load,
            total_days=total_days,
            steps=steps,
            source_exercises=[e.id for e in available_exercises],
            validation_errors=[],
            metadata={"created_at": datetime.utcnow().isoformat(), "composition_version": "mvp_v1"},
        )

        validation_errors = validate_plan_draft(draft)
        if validation_errors:
            raise DraftValidationError(validation_errors)

        return draft

    def _pick_category_for_slot(self, distribution: Dict[str, int], focus: Focus) -> str:
        """
        Pick category for current slot based on remaining distribution.
        Rule 3: Maintain 80/20 balance.
        """

        available = {k: v for k, v in distribution.items() if v > 0}

        if not available:
            return focus.value

        if focus.value in available:
            return focus.value

        return sorted(available.items(), key=lambda item: (-item[1], item[0]))[0][0]


def create_plan_draft(
    duration: str,
    focus: str,
    load: str,
    library_path: str,
    user_policy: dict | None = None,
    user_id: str = "",
) -> dict:
    """
    Convenience function for creating plan draft.

    Args:
        duration: "SHORT" | "STANDARD" | "LONG"
        focus: "somatic" | "cognitive" | "boundaries" | "rest" | "mixed"
        load: "LITE" | "MID" | "INTENSIVE"
        library_path: Path to content library JSON
        user_policy: Optional dict with forbidden categories/areas

    Returns:
        dict representation of PlanDraft
    """

    from app.plan_drafts.plan_types import UserPolicy

    params = PlanParameters(
        duration=Duration(duration),
        focus=Focus(focus),
        load=Load(load),
        user_policy=UserPolicy(**user_policy) if user_policy else None,
    )

    library = ContentLibrary(library_path)

    builder = DraftBuilder(library, user_id=user_id)
    draft = builder.build_plan_draft(params)

    return {
        "id": draft.id,
        "duration": draft.duration.value,
        "focus": draft.focus.value,
        "load": draft.load.value,
        "total_days": draft.total_days,
        "total_steps": len(draft.steps),
        "steps": [
            {
                "step_id": s.step_id,
                "day_number": s.day_number,
                "exercise_id": s.exercise_id,
                "exercise_name": s.exercise_name,
                "category": s.category,
                "impact_areas": s.impact_areas,
                "slot_type": s.slot_type.value,
                "time_slot": s.time_slot.value,
                "difficulty": s.difficulty,
                "energy_cost": s.energy_cost,
            }
            for s in draft.steps
        ],
        "is_valid": draft.is_valid(),
        "validation_errors": draft.validation_errors,
        "metadata": draft.metadata,
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 5:
        print("Usage: python draft_builder.py <duration> <focus> <load> <library_path>")
        print("Example: python draft_builder.py STANDARD somatic MID content_library.json")
        sys.exit(1)

    duration = sys.argv[1]
    focus = sys.argv[2]
    load = sys.argv[3]
    library_path = sys.argv[4]

    result = create_plan_draft(duration, focus, load, library_path)

    print(json.dumps(result, indent=2, ensure_ascii=False))
