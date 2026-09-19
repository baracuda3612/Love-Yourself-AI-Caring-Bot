"""Plan draft types and the internal builder used by ``app.lifecycle``.

Lifecycle mutations are not exported from this package. Runtime callers use
``app.lifecycle.activate_plan``.

Builder (direct import only — do NOT import via this __init__):
    from app.plan_drafts.plan_builder_v5 import PlanBuilderV5, get_default_builder

Shared types:
    from app.plan_drafts.plan_types import Duration, Mechanic, TimeSlot
"""

from app.plan_drafts.plan_types import Duration, Mechanic, TimeSlot

__all__ = [
    "Duration",
    "Mechanic",
    "TimeSlot",
]
