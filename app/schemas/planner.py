from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field

class PlanModule(str, Enum):
    """Plan module identifiers stored in the database."""
    BURNOUT_RECOVERY = "burnout_recovery"
    SLEEP_OPTIMIZATION = "sleep_optimization"
    DIGITAL_DETOX = "digital_detox"

class StepType(str, Enum):
    """Step categories stored in the database."""
    ACTION = "action"
    REFLECTION = "reflection"
    REST = "rest"

class DifficultyLevel(str, Enum):
    """Difficulty levels stored in the database."""
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"

class TimeSlot(str, Enum):
    """Time slots used by the scheduler."""
    MORNING = "MORNING"
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
