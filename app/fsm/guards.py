"""FSM transition guards.

T5.3: Clean minimal guard set. No tunnels.

Live transitions:
  IDLE_NEW / ONBOARDING:* → IDLE_ONBOARDED     (onboarding completion)
  IDLE_ONBOARDED / post-plan IDLE → ACTIVE      (create_plan())
  ACTIVE ↔ ACTIVE_PAUSED                        (pause / resume)
  ACTIVE / ACTIVE_PAUSED → IDLE_FINISHED        (plan completes naturally)
  ACTIVE / ACTIVE_PAUSED → IDLE_PLAN_ABORTED    (cancel_plan())
  ACTIVE / ACTIVE_PAUSED → IDLE_DROPPED         (background expiry)
  ACTIVE / ACTIVE_PAUSED ↔ SCHEDULE_ADJUSTMENT  (time slot change)
"""

from __future__ import annotations

from app.fsm.states import (
    PLAN_CREATION_ENTRY_STATES,
    SCHEDULE_ADJUSTMENT_ALLOWED_TRANSITIONS,
)

_PLAN_END_STATES = {"IDLE_FINISHED", "IDLE_PLAN_ABORTED", "IDLE_DROPPED"}


def can_transition(from_state: str, to_state: str) -> bool:
    """Return True if the FSM transition from_state → to_state is allowed."""

    # Onboarding completion → IDLE_ONBOARDED
    if to_state == "IDLE_ONBOARDED" and (
        from_state == "IDLE_NEW" or from_state.startswith("ONBOARDING:")
    ):
        return True

    # Plan creation: post-onboarding / post-plan IDLE → ACTIVE
    if to_state == "ACTIVE" and from_state in PLAN_CREATION_ENTRY_STATES:
        return True

    # Pause / resume
    if from_state == "ACTIVE" and to_state == "ACTIVE_PAUSED":
        return True
    if from_state == "ACTIVE_PAUSED" and to_state == "ACTIVE":
        return True

    # Plan end (completion, cancellation, background drop)
    if from_state in {"ACTIVE", "ACTIVE_PAUSED"} and to_state in _PLAN_END_STATES:
        return True

    # Schedule adjustment
    if (from_state, to_state) in SCHEDULE_ADJUSTMENT_ALLOWED_TRANSITIONS:
        return True

    return False
