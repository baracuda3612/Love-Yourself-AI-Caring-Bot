"""Plan draft builder modules.

v5 builder (P1 canonical):
    Import directly — do NOT import via this package init.
    Requires PyYAML (listed in requirements.txt).

    from app.plan_drafts.plan_builder_v5 import PlanBuilderV5, get_default_builder

Legacy builder (orchestrator / adaptation code):
    Kept for backward compat. Broken with v5 library JSON — see T5.2.
"""

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
    # shared types (v5-compatible)
    "Duration",
    "Mechanic",
    "TimeSlot",
    # legacy (kept for backward compat — do not use in new code)
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
