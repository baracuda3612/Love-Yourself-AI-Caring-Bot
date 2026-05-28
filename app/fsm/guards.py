"""FSM transition guards."""

from __future__ import annotations

from app.fsm.states import (
    ADAPTATION_CONFIRMATION,
    ADAPTATION_FLOW_ALLOWED_TRANSITIONS,
    ADAPTATION_FLOW_STATES,
    ADAPTATION_SELECTION,
    PLAN_CREATION_ENTRY_STATES,
    SCHEDULE_ADJUSTMENT_ALLOWED_TRANSITIONS,
    PLAN_FLOW_ENTRYPOINTS,
    PLAN_FLOW_STATES,
)


def can_transition(from_state: str, to_state: str) -> bool:
    """Validate whether FSM transition is allowed.

    Invariants:
    - No direct PLAN_FLOW <-> ADAPTATION_FLOW tunnel crossing.
    - Each tunnel can only progress via its own transition table.
    - IDLE_* → ACTIVE: allowed for direct plan creation via create_plan().
    """

    if from_state in PLAN_FLOW_STATES and to_state in ADAPTATION_FLOW_STATES:
        return False
    if from_state in ADAPTATION_FLOW_STATES and to_state in PLAN_FLOW_STATES:
        return False

    if (from_state, to_state) in ADAPTATION_FLOW_ALLOWED_TRANSITIONS:
        return True

    if (from_state, to_state) in SCHEDULE_ADJUSTMENT_ALLOWED_TRANSITIONS:
        return True

    if to_state == ADAPTATION_SELECTION and from_state in {"ACTIVE", "ACTIVE_PAUSED"}:
        return True
    if from_state in ADAPTATION_FLOW_STATES and to_state == "ACTIVE":
        return True
    if from_state == ADAPTATION_CONFIRMATION and to_state == "ACTIVE_PAUSED":
        return True

    if to_state == "PLAN_FLOW:DATA_COLLECTION" and from_state in PLAN_FLOW_ENTRYPOINTS:
        return True

    # T5.3: direct IDLE_* → ACTIVE for create_plan() path.
    # No intermediate PLAN_FLOW:FINALIZATION step — plan goes directly to ACTIVE.
    if to_state == "ACTIVE" and from_state in PLAN_CREATION_ENTRY_STATES:
        return True

    if from_state in PLAN_FLOW_STATES and to_state == "IDLE_PLAN_ABORTED":
        return True

    return False
