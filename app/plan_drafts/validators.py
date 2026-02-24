"""
Validation rules for plan drafts.
These are deterministic checks - no AI interpretation.
"""

from typing import List

from app.plan_drafts.plan_types import Load, PlanDraft, PlanParameters, PlanStep, SlotType, TimeSlot


class ValidationError:
    """Structured validation error"""

    DURATION_MISSING = "DURATION_MISSING"
    FOCUS_MISSING = "FOCUS_MISSING"
    LOAD_MISSING = "LOAD_MISSING"
    INVALID_TIME_SLOT = "INVALID_TIME_SLOT"
    MISSING_TIME_SLOT = "MISSING_TIME_SLOT"
    INVALID_SLOT_DISTRIBUTION = "INVALID_SLOT_DISTRIBUTION"
    CONSECUTIVE_DUPLICATE = "CONSECUTIVE_DUPLICATE"
    EMPTY_PLAN = "EMPTY_PLAN"
    WRONG_TOTAL_DAYS = "WRONG_TOTAL_DAYS"


def validate_three_pillars(params: PlanParameters) -> List[str]:
    """
    Rule 2: THE "THREE PILLARS" PREREQUISITE
    DO NOT generate a plan unless these three variables are defined.
    """

    errors = []

    if not params.duration:
        errors.append(ValidationError.DURATION_MISSING)
    if not params.focus:
        errors.append(ValidationError.FOCUS_MISSING)
    if not params.load:
        errors.append(ValidationError.LOAD_MISSING)

    return errors


def validate_time_slots(steps: List[PlanStep]) -> List[str]:
    """
    Rule 11: TIME SLOT CONTRACT
    Each plan step MUST be assigned to exactly one time_slot.
    """

    errors = []

    allowed_slots = {TimeSlot.MORNING, TimeSlot.DAY, TimeSlot.EVENING}

    for step in steps:
        if not step.time_slot:
            errors.append(f"{ValidationError.MISSING_TIME_SLOT}: step {step.step_id}")
        elif step.time_slot not in allowed_slots:
            errors.append(f"{ValidationError.INVALID_TIME_SLOT}: {step.time_slot} in step {step.step_id}")

    return errors


def validate_slot_distribution(steps: List[PlanStep], load: Load) -> List[str]:
    """
    Rule 6: LOAD MATRIX (Slot Allocation)
    Validate that each day has correct number of slots per load mode.

    LITE: 1 Slot (CORE)
    MID: 2 Slots (1 CORE + 1 SUPPORT)
    INTENSIVE: 3 Slots (1 CORE + 1 SUPPORT + 1 EMERGENCY/REST)
    """

    errors = []

    expected_slots = {Load.LITE: 1, Load.MID: 2, Load.INTENSIVE: 3}

    expected = expected_slots.get(load, 1)

    steps_by_day = {}
    for step in steps:
        steps_by_day.setdefault(step.day_number, []).append(step)

    for day, day_steps in steps_by_day.items():
        actual = len(day_steps)
        if actual != expected:
            errors.append(
                f"{ValidationError.INVALID_SLOT_DISTRIBUTION}: "
                f"Day {day} has {actual} slots, expected {expected} for {load.value}"
            )

        if load == Load.MID:
            slot_types = [s.slot_type for s in day_steps]
            if SlotType.CORE not in slot_types or SlotType.SUPPORT not in slot_types:
                errors.append(
                    f"{ValidationError.INVALID_SLOT_DISTRIBUTION}: "
                    f"Day {day} missing required CORE and SUPPORT slots for MID load"
                )

        elif load == Load.INTENSIVE:
            slot_types = [s.slot_type for s in day_steps]
            required = {SlotType.CORE, SlotType.SUPPORT}
            emergency_or_rest = {SlotType.EMERGENCY, SlotType.REST}

            if not required.issubset(slot_types):
                errors.append(
                    f"{ValidationError.INVALID_SLOT_DISTRIBUTION}: "
                    f"Day {day} missing CORE or SUPPORT for INTENSIVE load"
                )

            if not any(st in emergency_or_rest for st in slot_types):
                errors.append(
                    f"{ValidationError.INVALID_SLOT_DISTRIBUTION}: "
                    f"Day {day} missing EMERGENCY or REST slot for INTENSIVE load"
                )

    return errors


def validate_no_consecutive_duplicates(steps: List[PlanStep]) -> List[str]:
    """
    Rule 4: DYNAMIC ROTATION & COOLDOWN
    AVOID repeating the exact same exercise_id on consecutive days.
    """

    errors = []

    sorted_steps = sorted(steps, key=lambda s: (s.day_number, s.step_id))

    last_day_exercises = {}

    for step in sorted_steps:
        day = step.day_number
        exercise_id = step.exercise_id

        if day - 1 in last_day_exercises:
            if exercise_id in last_day_exercises[day - 1]:
                errors.append(
                    f"{ValidationError.CONSECUTIVE_DUPLICATE}: "
                    f"Exercise {exercise_id} used on consecutive days {day-1} and {day}"
                )

        last_day_exercises.setdefault(day, []).append(exercise_id)

    return errors


def validate_plan_draft(draft: PlanDraft) -> List[str]:
    """
    Master validation function.
    Runs all validation rules and returns combined errors.
    """

    errors = []

    if not draft.steps or len(draft.steps) == 0:
        errors.append(ValidationError.EMPTY_PLAN)
        return errors

    expected_days = {
        "SHORT": (7, 7),
        "MEDIUM": (14, 14),
        "STANDARD": (21, 21),
        "LONG": (90, 90),
    }

    duration_range = expected_days.get(draft.duration.value, (0, 0))
    if not (duration_range[0] <= draft.total_days <= duration_range[1]):
        errors.append(
            f"{ValidationError.WRONG_TOTAL_DAYS}: "
            f"Got {draft.total_days} days, expected {duration_range} for {draft.duration.value}"
        )

    errors.extend(validate_time_slots(draft.steps))
    errors.extend(validate_slot_distribution(draft.steps, draft.load))
    errors.extend(validate_no_consecutive_duplicates(draft.steps))

    return errors


def get_clarifying_questions(params: PlanParameters) -> List[str]:
    """
    Generate questions for missing pillars.
    Used by orchestrator to ask user for missing data.
    """

    questions = []

    missing = params.missing_pillars()

    if "duration" in missing:
        questions.append(
            "Як довго ти хочеш займатися практиками? "
            "Обери: SHORT (7 днів), MEDIUM (14 днів), STANDARD (21 день) або LONG (90 днів)"
        )

    if "focus" in missing:
        questions.append(
            "На чому хочеш зосередитися? "
            "Обери: Тіло (somatic), Розум (cognitive), Межі (boundaries), Відпочинок (rest) або Комбінація (mixed)"
        )

    if "load" in missing:
        questions.append(
            "Скільки часу маєш щодня? "
            "Обери: LITE (1 завдання), MID (2 завдання) або INTENSIVE (3 завдання)"
        )

    return questions
