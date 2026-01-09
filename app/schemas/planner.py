from enum import Enum
from typing import List, Optional, Dict
from pydantic import BaseModel, Field

# --- ENUMS ---

class PlanModule(str, Enum):
    """Behavioral modules supported by the system."""
    BURNOUT_RECOVERY = "burnout_recovery"
    SLEEP_OPTIMIZATION = "sleep_optimization"
    DIGITAL_DETOX = "digital_detox"

class StepType(str, Enum):
    """Type of action for load balancing."""
    EDUCATION = "education"   # Learn/Read
    ACTION = "action"         # Do active task
    REFLECTION = "reflection" # Journaling/Thinking
    REST = "rest"             # Passive rest

class DifficultyLevel(str, Enum):
    """Adaptation lever."""
    EASY = "easy"     # 1-5 min, minimal effort
    MEDIUM = "medium" # 10-20 min
    HARD = "hard"     # 30+ min, high friction

class TimeSlot(str, Enum):
    """Structural time slots for scheduling."""
    MORNING = "MORNING"
    DAY = "DAY"
    EVENING = "EVENING"

# --- INPUT LAYER (Planner Blindness) ---

class UserPolicy(BaseModel):
    """User constraints and preferences."""
    blocked_hours: List[int] = Field(default_factory=list, description="Hours when notifications are forbidden (0-23)")
    
    # Weights: 0.0 = Hate/Block, 1.0 = Love/Prefer. 
    activity_preferences: Dict[str, float] = Field(
        default_factory=dict, 
        description="Preference weights, e.g., 'meditation': 0.1"
    )
    
    daily_step_target: int = Field(default=3, description="Recommended number of steps per day")

class FunctionalSnapshot(BaseModel):
    """Behavioral telemetry without emotional context."""
    completion_rate_7d: float = Field(..., description="0.0 - 1.0")
    skip_streak: int = Field(default=0, description="Consecutive days skipped")
    
    burnout_proxy_metric: Optional[int] = Field(
        None, 
        description="EXPERIMENTAL (0-100). Calculated heuristic, NOT a clinical score."
    )

class PlannerInputContext(BaseModel):
    """Full context payload for the Planner AI."""
    goal_text: str = Field(..., description="User's stated goal")
    user_timezone: str = Field(default="Europe/Kyiv")
    
    policy: UserPolicy
    telemetry: FunctionalSnapshot

# --- OUTPUT LAYER (Generated Cards) ---

class PlanStepSchema(BaseModel):
    title: str
    description: str
    step_type: StepType
    difficulty: DifficultyLevel
    estimated_minutes: int
    time_slot: TimeSlot = Field(..., description="MORNING, DAY, EVENING")

class PlanDaySchema(BaseModel):
    day_number: int
    focus_theme: Optional[str]
    steps: List[PlanStepSchema]

class MilestoneSchema(BaseModel):
    day_trigger: int
    description: str

class GeneratedPlan(BaseModel):
    """Strict JSON output structure from LLM."""
    title: str
    module_id: PlanModule
    reasoning: str = Field(..., description="Technical reasoning for logs")
    duration_days: int
    schedule: List[PlanDaySchema]
    milestones: List[MilestoneSchema] = Field(default_factory=list)
