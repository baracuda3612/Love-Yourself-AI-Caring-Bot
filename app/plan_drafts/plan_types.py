"""
Core type definitions for Plan Builder.
These are deterministic contracts - no LLM interpretation needed.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Duration(str, Enum):
    """Plan duration types"""

    SHORT = "SHORT"  # 7 days
    MEDIUM = "MEDIUM"  # 14 days
    STANDARD = "STANDARD"  # 21 days
    LONG = "LONG"  # 90 days


class Focus(str, Enum):
    """Focus categories matching content library"""

    SOMATIC = "somatic"
    COGNITIVE = "cognitive"
    BOUNDARIES = "boundaries"
    REST = "rest"
    MIXED = "mixed"


class Load(str, Enum):
    """Daily task load"""

    LITE = "LITE"  # 1 task per day
    MID = "MID"  # 2 tasks per day
    INTENSIVE = "INTENSIVE"  # 3 tasks per day


class SlotType(str, Enum):
    """Task slot types based on priority"""

    CORE = "CORE"
    SUPPORT = "SUPPORT"
    EMERGENCY = "EMERGENCY"
    REST = "REST"


class TimeSlot(str, Enum):
    """When to execute task"""

    MORNING = "MORNING"
    DAY = "DAY"
    EVENING = "EVENING"


@dataclass
class UserPolicy:
    """
    MVP version - limited set of constraints.
    Expandable later based on real user feedback.
    """

    forbidden_categories: list[str] = field(default_factory=list)
    forbidden_impact_areas: list[str] = field(default_factory=list)
    preferred_time_slots: list[str] = field(default_factory=list)

    def allows_category(self, category: str) -> bool:
        """Check if category is allowed"""

        return category.lower() not in [c.lower() for c in self.forbidden_categories]

    def allows_impact_area(self, impact_areas: list[str]) -> bool:
        """Check if any impact area is forbidden"""

        forbidden = [ia.lower() for ia in self.forbidden_impact_areas]
        return not any(ia.lower() in forbidden for ia in impact_areas)


@dataclass
class PlanParameters:
    """
    The "Three Pillars" + optional constraints.
    Draft builder CANNOT work without duration, focus, load.
    """

    duration: Optional[Duration] = None
    focus: Optional[Focus] = None
    load: Optional[Load] = None
    user_policy: Optional[UserPolicy] = None

    def is_complete(self) -> bool:
        """Check if all three pillars are defined"""

        return all([self.duration, self.focus, self.load])

    def missing_pillars(self) -> list[str]:
        """Return list of missing required parameters"""

        missing = []
        if not self.duration:
            missing.append("duration")
        if not self.focus:
            missing.append("focus")
        if not self.load:
            missing.append("load")
        return missing


@dataclass
class PlanStep:
    """
    Single task in the plan.
    This is what becomes a "plan card" later.
    """

    step_id: str
    day_number: int
    exercise_id: str
    exercise_name: str
    category: str
    impact_areas: list[str]
    slot_type: SlotType
    time_slot: TimeSlot
    difficulty: int
    energy_cost: str


@dataclass
class PlanDraft:
    """
    Complete plan draft artifact.
    This is stored in DB and shown to user for confirmation.
    """

    id: str
    duration: Duration
    focus: Focus
    load: Load
    total_days: int
    steps: list[PlanStep]
    source_exercises: list[str]  # Which content library exercises were used
    validation_errors: list[str]
    metadata: dict = field(default_factory=dict)

    def is_valid(self) -> bool:
        """Check if draft passed all validations"""

        return len(self.validation_errors) == 0

    def total_steps(self) -> int:
        """Total number of steps in plan"""

        return len(self.steps)

    def steps_per_day(self) -> float:
        """Average steps per day"""

        return len(self.steps) / self.total_days if self.total_days > 0 else 0


@dataclass
class Exercise:
    """
    Single exercise from content library.
    Simplified view for plan composition logic.
    """

    id: str
    internal_name: str
    category: str
    impact_areas: list[str]
    priority_tier: str
    difficulty: int
    energy_cost: str
    cooldown_days: int
    is_active: bool
    base_weight: float

    @staticmethod
    def from_library_item(item: dict) -> "Exercise":
        """Parse exercise from content library JSON"""

        logic = item["logic_tags"]
        balancing = item["balancing"]

        return Exercise(
            id=item["id"],
            internal_name=item["internal_name"],
            category=logic["category"],
            impact_areas=logic["impact_areas"],
            priority_tier=logic["priority_tier"],
            difficulty=logic["difficulty"],
            energy_cost=logic["energy_cost"],
            cooldown_days=balancing["cooldown_days"],
            is_active=balancing["is_active"],
            base_weight=balancing["base_weight"],
        )
