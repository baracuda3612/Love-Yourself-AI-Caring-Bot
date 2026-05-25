"""
Planner schemas — Pydantic models and enums for plan generation.

──────────────────────────────────────────────────────────────────────────────
DEPRECATION NOTICE (T5.1, 2026-05)
──────────────────────────────────────────────────────────────────────────────
StepType and DifficultyLevel are DEPRECATED.
They are kept here ONLY because ai_plan_steps DB columns use them as enum
types (step_type, difficulty). They must not be used in new builder code.

New builder: app.plan_drafts.plan_builder_v5.PlanBuilderV5
New mechanic enum: app.plan_drafts.plan_types.Mechanic
──────────────────────────────────────────────────────────────────────────────
"""

from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field


class PlanModule(str, Enum):
    """Plan module identifiers stored in the database."""
    BURNOUT_RECOVERY = "burnout_recovery"
    SLEEP_OPTIMIZATION = "sleep_optimization"
    DIGITAL_DETOX = "digital_detox"


class StepType(str, Enum):
    """
    DEPRECATED (T5.1) — do not use in new code.
    Kept for DB legacy: ai_plan_steps.step_type column uses this enum.
    """
    ACTION = "action"
    REFLECTION = "reflection"
    REST = "rest"


class DifficultyLevel(str, Enum):
    """
    DEPRECATED (T5.1) — do not use in new code.
    Kept for DB legacy: ai_plan_steps.difficulty column uses this enum.
    """
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class TimeSlot(str, Enum):
    """
    Time slots used by the scheduler.
    MORNING — not used in P1 plan recipes (frozen).
    """
    MORNING = "MORNING"  # FROZEN — not used in P1 plan recipes
    DAY = "DAY"
    EVENING = "EVENING"


class PlanStepSchema(BaseModel):
    exercise_id: Optional[str] = None
    title: str
    description: str
    step_type: StepType
    difficulty: DifficultyLevel
    time_slot: TimeSlot


class PlanDaySchema(BaseModel):
    day_number: int
    focus_theme: Optional[str]
    steps: List[PlanStepSchema]


class MilestoneSchema(BaseModel):
    day_trigger: int
    description: str


class GeneratedPlan(BaseModel):
    """JSON contract for Plan Agent output."""
    title: str
    module_id: PlanModule
    reasoning: str
    duration_days: int
    schedule: List[PlanDaySchema]
    milestones: List[MilestoneSchema] = Field(default_factory=list)
