"""Plan draft builder modules."""

# ── v5 builder (P1 canonical) ─────────────────────────────────────────────────
from app.plan_drafts.plan_builder_v5 import (
    ExerciseV5,
    InvalidRecipeError,
    MissingEveningSlotError,
    NoCandidatesError,
    PlanBuilderV5,
    PlanDraftV5,
    PlanStepV5,
    get_default_builder,
)

# ── Legacy builder (kept for orchestrator / adaptation code) ──────────────────
from app.plan_drafts.draft_builder import (
    ContentLibrary,
    DraftBuilder,
    DraftValidationError,
    InsufficientLibraryError,
)
from app.plan_drafts.plan_types import (
    Duration,
    Focus,
    Load,
    Mechanic,
    PlanDraft,
    PlanParameters,
    PlanStep,
    SlotType,
    TimeSlot,
    UserPolicy,
)

__all__ = [
    # v5 builder
    "ExerciseV5",
    "InvalidRecipeError",
    "MissingEveningSlotError",
    "NoCandidatesError",
    "PlanBuilderV5",
    "PlanDraftV5",
    "PlanStepV5",
    "get_default_builder",
    # shared types
    "Duration",
    "Mechanic",
    "TimeSlot",
    # legacy (kept for backward compat)
    "ContentLibrary",
    "DraftBuilder",
    "DraftValidationError",
    "InsufficientLibraryError",
    "Focus",
    "Load",
    "PlanDraft",
    "PlanParameters",
    "PlanStep",
    "SlotType",
    "UserPolicy",
]
