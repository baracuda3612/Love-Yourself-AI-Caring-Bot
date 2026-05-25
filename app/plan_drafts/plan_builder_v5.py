"""
Plan Builder v5 — mechanic-based, config-driven.

Canonical builder for P1 plans (SHORT and MEDIUM).
Replaces the Focus/Load/Category-based builder for new plan generation.

Critical invariants (product_internal_spec.md v2.0):
  1. Builder uses only plan_context_template.yaml + library.mechanic.
  2. Builder must not use Focus, Load, StepType, SlotType, DifficultyLevel.
  3. First plan always SHORT.
  4. MEDIUM requires both DAY and EVENING slots for every active day.
  5. MEDIUM requires an existing valid EVENING HH:MM; no silent default.
  6. mechanic on ai_plan_steps is a snapshot — never recomputed at delivery.
  7. Pause is not an adaptation and does not rewrite the plan.
  8. No new adaptation records while ADAPTATIONS_ENABLED=False.
"""

from __future__ import annotations

import json
import random
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml


# ─── Data structures ────────────────────────────────────────────────────────


@dataclass
class ExerciseV5:
    """Exercise from content library v5 schema."""

    id: str
    mechanic: str          # "switch" | "unload"
    cooldown_days: int
    weight: float
    is_active: bool
    # display fields — for rendering, not used by builder selection logic
    title: str
    steps: List[str]
    duration_minutes: int
    extended_minutes: Optional[int]

    @staticmethod
    def from_library_item(item: dict) -> "ExerciseV5":
        display = item.get("display", {})
        return ExerciseV5(
            id=item["id"],
            mechanic=item["mechanic"],
            cooldown_days=item["cooldown_days"],
            weight=float(item.get("weight", 1.0)),
            is_active=bool(item.get("is_active", True)),
            title=display.get("title", ""),
            steps=list(display.get("steps", [])),
            duration_minutes=int(item.get("duration_minutes", 1)),
            extended_minutes=item.get("extended_minutes"),
        )


@dataclass
class PlanStepV5:
    """Single scheduled step in a v5 plan draft."""

    step_id: str
    day_number: int
    time_slot: str         # "DAY" | "EVENING"
    mechanic: str          # snapshot from exercise.mechanic — never recomputed
    exercise_id: str


@dataclass
class PlanDraftV5:
    """Complete v5 plan draft artifact."""

    id: str
    plan_type: str         # "SHORT" | "MEDIUM"
    active_days_count: int
    steps: List[PlanStepV5]
    source_exercises: List[str]
    metadata: dict = field(default_factory=dict)

    def total_steps(self) -> int:
        return len(self.steps)

    def steps_for_day(self, day: int) -> List[PlanStepV5]:
        return [s for s in self.steps if s.day_number == day]


# ─── Errors ──────────────────────────────────────────────────────────────────


class NoCandidatesError(RuntimeError):
    """No exercise available for a required slot — always fail loudly, never silently skip."""


class InvalidRecipeError(ValueError):
    """plan_context_template.yaml is invalid or the requested plan_type is unknown."""


class MissingEveningSlotError(ValueError):
    """MEDIUM plan requires evening_time but none was provided (invariant 5)."""


# ─── Builder ─────────────────────────────────────────────────────────────────


class PlanBuilderV5:
    """
    Mechanic-based plan builder — config-driven, deterministic.

    Reads plan recipes from plan_context_template.yaml.
    Reads exercises from the content library JSON.
    Does NOT use Focus, Load, StepType, SlotType, or DifficultyLevel.
    """

    def __init__(
        self,
        library_path: str | Path,
        recipe_path: str | Path,
    ) -> None:
        self.exercises: List[ExerciseV5] = self._load_library(Path(library_path))
        self.recipes: dict = self._load_recipe(Path(recipe_path))

    # ── Loading ──────────────────────────────────────────────────────────────

    @staticmethod
    def _load_library(path: Path) -> List[ExerciseV5]:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return [
            ExerciseV5.from_library_item(item)
            for item in data.get("inventory", [])
        ]

    @staticmethod
    def _load_recipe(path: Path) -> dict:
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        # Strip YAML comments and return only plan type keys
        return {k: v for k, v in raw.items() if isinstance(v, dict)}

    # ── Public API ───────────────────────────────────────────────────────────

    def build(
        self,
        plan_type: str,
        user_id: str = "",
        day_time: str = "14:00",
        evening_time: Optional[str] = None,
        active_days: Optional[List[str]] = None,  # reserved for scheduling layer
    ) -> PlanDraftV5:
        """
        Build a plan draft.

        Args:
            plan_type:    "SHORT" or "MEDIUM"
            user_id:      used for deterministic seed per user
            day_time:     HH:MM from user_profiles.daily_time_slots["DAY"]
            evening_time: HH:MM from user_profiles.daily_time_slots["EVENING"]
                          Required for MEDIUM. Must not be None or empty.
            active_days:  reserved — weekday routing is handled by scheduling layer

        Returns:
            PlanDraftV5

        Raises:
            InvalidRecipeError       if plan_type is unknown
            MissingEveningSlotError  if MEDIUM plan and evening_time is None
            NoCandidatesError        if no exercise is available for any slot
        """
        plan_type = plan_type.upper()

        if plan_type not in self.recipes:
            raise InvalidRecipeError(
                f"Unknown plan_type '{plan_type}'. "
                f"Available: {list(self.recipes.keys())}"
            )

        recipe = self.recipes[plan_type]
        active_days_count: int = recipe["active_days_count"]
        slot_configs: List[dict] = recipe["slots"]

        # Invariant 5: MEDIUM must have a valid EVENING HH:MM — no silent default
        evening_slots = [s for s in slot_configs if s["slot"] == "EVENING"]
        if evening_slots and not evening_time:
            raise MissingEveningSlotError(
                f"{plan_type} plan requires a valid evening_time "
                f"(daily_time_slots['EVENING']) but none was provided."
            )

        active = [e for e in self.exercises if e.is_active]
        if not active:
            raise NoCandidatesError("Content library has no active exercises")

        last_used: Dict[str, int] = {}
        steps: List[PlanStepV5] = []

        for day in range(1, active_days_count + 1):
            for slot_config in slot_configs:
                slot: str = slot_config["slot"]                # "DAY" | "EVENING"
                preferred: str = slot_config["preferred_mechanic"]
                fallback: Optional[str] = slot_config.get("fallback_mechanic")

                exercise = self._pick_exercise(
                    active=active,
                    preferred_mechanic=preferred,
                    fallback_mechanic=fallback,
                    current_day=day,
                    last_used=last_used,
                    seed_key=f"{user_id}:{day}:{slot}",
                    context=f"plan_type={plan_type}, day={day}, slot={slot}",
                )

                steps.append(PlanStepV5(
                    step_id=f"d{day}_{slot.lower()}",
                    day_number=day,
                    time_slot=slot,
                    mechanic=exercise.mechanic,   # Invariant 6: snapshotted here
                    exercise_id=exercise.id,
                ))

                last_used[exercise.id] = day  # track cooldown

        return PlanDraftV5(
            id=str(uuid.uuid4()),
            plan_type=plan_type,
            active_days_count=active_days_count,
            steps=steps,
            source_exercises=[e.id for e in active],
            metadata={
                "builder_version": "v5",
                "user_id": user_id,
                "day_time": day_time,
                "evening_time": evening_time,
            },
        )

    # ── Internal ─────────────────────────────────────────────────────────────

    @staticmethod
    def _is_in_cooldown(
        exercise_id: str,
        current_day: int,
        cooldown_days: int,
        last_used: Dict[str, int],
    ) -> bool:
        if exercise_id not in last_used:
            return False
        return (current_day - last_used[exercise_id]) <= cooldown_days

    def _candidates(
        self,
        active: List[ExerciseV5],
        mechanic: str,
        current_day: int,
        last_used: Dict[str, int],
    ) -> List[ExerciseV5]:
        return [
            e for e in active
            if e.mechanic == mechanic
            and not self._is_in_cooldown(e.id, current_day, e.cooldown_days, last_used)
        ]

    def _pick_exercise(
        self,
        active: List[ExerciseV5],
        preferred_mechanic: str,
        fallback_mechanic: Optional[str],
        current_day: int,
        last_used: Dict[str, int],
        seed_key: str,
        context: str,
    ) -> ExerciseV5:
        candidates = self._candidates(active, preferred_mechanic, current_day, last_used)

        if not candidates and fallback_mechanic:
            candidates = self._candidates(active, fallback_mechanic, current_day, last_used)

        if not candidates:
            # Invariants 4 & 5: MEDIUM must always find candidates — fail loudly
            raise NoCandidatesError(
                f"No exercise available ({context}). "
                f"preferred={preferred_mechanic!r}, fallback={fallback_mechanic!r}. "
                f"Library may be too small or all exercises are in cooldown."
            )

        return self._weighted_choice(candidates, seed_key=seed_key)

    @staticmethod
    def _weighted_choice(exercises: List[ExerciseV5], seed_key: str = "") -> ExerciseV5:
        """Seeded weighted random selection — same seed produces same result."""
        pool = sorted(exercises, key=lambda e: e.id)  # deterministic sort before rng
        weights = [e.weight for e in pool]
        rng = random.Random(seed_key)
        return rng.choices(pool, weights=weights, k=1)[0]


# ─── Default paths ───────────────────────────────────────────────────────────

_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_LIBRARY_PATH: Path = (
    _ROOT / "resource" / "assets" / "content_library" / "tasks"
    / "burnout_combined_content_library.json"
)
DEFAULT_RECIPE_PATH: Path = (
    _ROOT / "resource" / "assets" / "plan" / "plan_context_template.yaml"
)


def get_default_builder() -> PlanBuilderV5:
    """Return a PlanBuilderV5 loaded from the default asset paths."""
    return PlanBuilderV5(DEFAULT_LIBRARY_PATH, DEFAULT_RECIPE_PATH)
