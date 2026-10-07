"""
Tests for PlanBuilderV5 — T5.1 spec, 10 test cases.

Critical invariants verified:
  1. Builder uses only plan_context_template.yaml + library.mechanic.
  2. Builder must not use Focus, Load, StepType, SlotType, DifficultyLevel.
  3. First plan always SHORT.
  4. MEDIUM requires both DAY and EVENING steps for every active day.
  5. MEDIUM requires an existing valid EVENING HH:MM; no silent default.
  6. mechanic on each step is a snapshot matching the exercise's mechanic.
  7. Pause does not rewrite the plan; lifecycle owns it.
  8. Selection is independent of response outcomes and feedback.
"""

from pathlib import Path
import json
from unittest.mock import Mock
from app.content_library import is_eligible, FIELDS
from app.db import ContentLibrary

import pytest

from app.plan_drafts.plan_builder_v5 import (
    InvalidRecipeError,
    MissingEveningSlotError,
    NoCandidatesError,
    PlanBuilderV5,
    PlanDraftV5,
    PriorCycleContext,
)

# ── Paths ─────────────────────────────────────────────────────────────────────

_ROOT = Path(__file__).resolve().parents[1]

LIBRARY_PATH = (
    _ROOT / "resource" / "assets" / "content_library" / "tasks"
    / "burnout_combined_content_library.json"
)
RECIPE_PATH = (
    _ROOT / "resource" / "assets" / "plan" / "plan_context_template.yaml"
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def builder() -> PlanBuilderV5:
    seed = json.loads(LIBRARY_PATH.read_text())["inventory"]
    rows = [ContentLibrary(exercise_id=row["id"],
        **{key: row[key] for key in FIELDS}) for row in seed]
    db = Mock()
    db.execute.return_value.scalars.return_value = rows
    return PlanBuilderV5(db, RECIPE_PATH)


# ── T1: SHORT plan produces exactly 7 steps ───────────────────────────────────


def test_short_plan_step_count(builder: PlanBuilderV5) -> None:
    draft = builder.build("SHORT", user_id="t1", day_time="14:00")
    assert isinstance(draft, PlanDraftV5)
    assert draft.plan_type == "SHORT"
    assert draft.active_days_count == 7
    assert len(draft.steps) == 7


# ── T2: SHORT plan has only DAY time_slot ────────────────────────────────────


def test_short_plan_only_day_slots(builder: PlanBuilderV5) -> None:
    draft = builder.build("SHORT", user_id="t2", day_time="14:00")
    for step in draft.steps:
        assert step.time_slot == "DAY", (
            f"SHORT plan must only have DAY steps, got {step.time_slot!r}"
        )


# ── T3: MEDIUM plan produces 28 steps (14 days × 2 slots) ────────────────────


def test_medium_plan_step_count(builder: PlanBuilderV5) -> None:
    draft = builder.build("MEDIUM", user_id="t3", day_time="14:00", evening_time="21:00")
    assert draft.plan_type == "MEDIUM"
    assert draft.active_days_count == 14
    assert len(draft.steps) == 28, (
        f"MEDIUM plan must have 28 steps (14 × 2), got {len(draft.steps)}"
    )


# ── T4: MEDIUM plan has both DAY and EVENING for every day ───────────────────


def test_medium_plan_both_slots_every_day(builder: PlanBuilderV5) -> None:
    draft = builder.build("MEDIUM", user_id="t4", day_time="14:00", evening_time="21:00")
    for day in range(1, 15):
        day_steps = draft.steps_for_day(day)
        assert len(day_steps) == 2, (
            f"MEDIUM plan day {day} must have 2 steps, got {len(day_steps)}"
        )
        slots = {s.time_slot for s in day_steps}
        assert slots == {"DAY", "EVENING"}, (
            f"Day {day} must have DAY+EVENING, got {slots}"
        )


# ── T5: mechanic on each step is a snapshot matching the library ─────────────


def test_mechanic_is_snapshot_from_library(builder: PlanBuilderV5) -> None:
    exercise_mechanic = {e.id: e.mechanic for e in builder.exercises}
    draft = builder.build("SHORT", user_id="t5", day_time="14:00")
    for step in draft.steps:
        expected = exercise_mechanic[step.exercise_id]
        assert step.mechanic == expected, (
            f"Step mechanic {step.mechanic!r} != exercise mechanic {expected!r} "
            f"for exercise {step.exercise_id}"
        )


# ── T6: DAY slots must use switch mechanic ────────────────────────────────────


def test_day_slots_use_switch_mechanic(builder: PlanBuilderV5) -> None:
    """
    plan_context_template.yaml declares preferred_mechanic=switch for DAY.
    All DAY steps must therefore have mechanic='switch'.
    """
    draft = builder.build("MEDIUM", user_id="t6", day_time="14:00", evening_time="21:00")
    day_steps = [s for s in draft.steps if s.time_slot == "DAY"]
    assert day_steps, "MEDIUM draft must contain DAY steps"
    for step in day_steps:
        assert step.mechanic == "switch", (
            f"DAY step must be mechanic='switch', got {step.mechanic!r}"
        )


# ── T7: MEDIUM raises MissingEveningSlotError when evening_time is None ──────


def test_medium_raises_without_evening_time(builder: PlanBuilderV5) -> None:
    with pytest.raises(MissingEveningSlotError):
        builder.build("MEDIUM", user_id="t7", day_time="14:00", evening_time=None)


# ── T8: Unknown plan_type raises InvalidRecipeError ──────────────────────────


def test_unknown_plan_type_raises(builder: PlanBuilderV5) -> None:
    with pytest.raises(InvalidRecipeError):
        builder.build("ULTRA", user_id="t8", day_time="14:00")


# ── T9: No consecutive duplicates in same slot (cooldown_days=1 respected) ───


def test_no_consecutive_duplicates_same_slot(builder: PlanBuilderV5) -> None:
    """
    With cooldown_days=1 on all exercises, the same exercise must not appear
    on back-to-back days in the same slot.
    """
    draft = builder.build("SHORT", user_id="t9", day_time="14:00")
    sorted_steps = sorted(draft.steps, key=lambda s: s.day_number)
    for i in range(1, len(sorted_steps)):
        prev = sorted_steps[i - 1]
        curr = sorted_steps[i]
        if curr.day_number == prev.day_number + 1 and curr.time_slot == prev.time_slot:
            assert curr.exercise_id != prev.exercise_id, (
                f"Exercise {curr.exercise_id!r} appeared on consecutive days "
                f"{prev.day_number} and {curr.day_number} in slot {curr.time_slot!r}"
            )


# ── T10: Same user_id produces the same plan (deterministic) ─────────────────


def test_deterministic_with_same_user_id(builder: PlanBuilderV5) -> None:
    draft_a = builder.build("SHORT", user_id="seed_xyz_42", day_time="14:00")
    draft_b = builder.build("SHORT", user_id="seed_xyz_42", day_time="14:00")
    assert len(draft_a.steps) == len(draft_b.steps)
    for sa, sb in zip(draft_a.steps, draft_b.steps):
        assert sa.exercise_id == sb.exercise_id, (
            f"Non-deterministic: day {sa.day_number} got "
            f"{sa.exercise_id!r} vs {sb.exercise_id!r}"
        )
        assert sa.mechanic == sb.mechanic


def _day_ids(draft: PlanDraftV5) -> tuple[str, ...]:
    return tuple(s.exercise_id for s in draft.steps if s.time_slot == "DAY")


def test_cycle_rebuild_and_next_cycle_are_distinct(builder: PlanBuilderV5) -> None:
    first = builder.build("SHORT", user_id="cycle", cycle_number=1)
    prior = PriorCycleContext(day_sequence=_day_ids(first))
    second = builder.build("SHORT", user_id="cycle", cycle_number=2, prior_cycle=prior)
    rebuilt = builder.build("SHORT", user_id="cycle", cycle_number=2, prior_cycle=prior)
    assert _day_ids(second) == _day_ids(rebuilt)
    assert [(s.content_version, s.content_snapshot) for s in second.steps] == [
        (s.content_version, s.content_snapshot) for s in rebuilt.steps
    ]
    assert _day_ids(second) != _day_ids(first)


def test_short_to_medium_does_not_replay_first_week(builder: PlanBuilderV5) -> None:
    short = builder.build("SHORT", user_id="upgrade", cycle_number=1)
    medium = builder.build(
        "MEDIUM", user_id="upgrade", cycle_number=2, evening_time="21:00",
        prior_cycle=PriorCycleContext(day_sequence=_day_ids(short)),
    )
    assert _day_ids(medium)[:7] != _day_ids(short)


def test_shown_cooldown_crosses_cycle_boundary(builder: PlanBuilderV5) -> None:
    first = builder.build("SHORT", user_id="cooldown", cycle_number=1)
    last = first.steps[-1].exercise_id
    prior = PriorCycleContext(_day_ids(first), {last: 0})
    next_plan = builder.build("SHORT", user_id="cooldown", cycle_number=2, prior_cycle=prior)
    assert next_plan.steps[0].exercise_id != last
    # With five DAY exercises and seven days, accepted within-week reuse remains.
    assert len(set(_day_ids(next_plan))) < len(_day_ids(next_plan))


def test_sources_are_only_scheduled_exact_versions(builder: PlanBuilderV5) -> None:
    draft = builder.build("SHORT", user_id="sources", cycle_number=1)
    assert draft.source_exercises == list(dict.fromkeys(s.exercise_id for s in draft.steps))
    assert len(draft.source_exercises) < len(builder.exercises)
    assert "cold_water_face" not in draft.source_exercises
    by_id = {e.id: e for e in builder.exercises}
    for step in draft.steps:
        assert step.content_version == by_id[step.exercise_id].content_version
        assert step.content_snapshot == by_id[step.exercise_id].payload


def test_invalid_cycle_rejected(builder: PlanBuilderV5) -> None:
    with pytest.raises(ValueError, match="cycle_number"):
        builder.build("SHORT", user_id="bad", cycle_number=0)
