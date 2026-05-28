"""FSM state definitions.

T5.3: Adaptation flow and Plan flow tunnels fully removed.
Active FSM (9 states):

  IDLE_NEW → ONBOARDING:* → IDLE_ONBOARDED → ACTIVE
  ACTIVE ↔ ACTIVE_PAUSED
  ACTIVE / ACTIVE_PAUSED → IDLE_FINISHED / IDLE_PLAN_ABORTED / IDLE_DROPPED
  IDLE_FINISHED / IDLE_PLAN_ABORTED / IDLE_DROPPED → ACTIVE
  ACTIVE / ACTIVE_PAUSED ↔ SCHEDULE_ADJUSTMENT  (future: call tool — backlog)

T5.4 backlog: delete adaptation_executor.py, orchestrator adaptation blocks,
              ai_plans adaptation prompts/tools, related tests.
"""

from __future__ import annotations

SCHEDULE_ADJUSTMENT = "SCHEDULE_ADJUSTMENT"

SCHEDULE_ADJUSTMENT_ALLOWED_TRANSITIONS = {
    ("ACTIVE", SCHEDULE_ADJUSTMENT),
    ("ACTIVE_PAUSED", SCHEDULE_ADJUSTMENT),
    (SCHEDULE_ADJUSTMENT, "ACTIVE"),
    (SCHEDULE_ADJUSTMENT, "ACTIVE_PAUSED"),
}

# States from which a new plan can be created via create_plan().
# IDLE_NEW excluded — users must complete onboarding first.
PLAN_CREATION_ENTRY_STATES = {
    "IDLE_ONBOARDED",
    "IDLE_FINISHED",
    "IDLE_DROPPED",
    "IDLE_PLAN_ABORTED",
}

IDLE_STATES = {
    "IDLE_NEW",
    "IDLE_ONBOARDED",
    "IDLE_PLAN_ABORTED",
    "IDLE_FINISHED",
    "IDLE_DROPPED",
}

ACTIVE_STATES = {"ACTIVE"}

PAUSE_STATES = {"ACTIVE_PAUSED"}

ALLOWED_BASE_STATES = IDLE_STATES | ACTIVE_STATES | PAUSE_STATES

FSM_ALLOWED_STATES = ALLOWED_BASE_STATES | {SCHEDULE_ADJUSTMENT}


def is_valid_fsm_state(state: str) -> bool:
    """Return True for any valid FSM state, including ONBOARDING:* wildcards."""
    if state in FSM_ALLOWED_STATES:
        return True
    return any(state.startswith(prefix + ":") for prefix in PREFIXED_STATES)

# States where the plan agent may be invoked (for plan creation).
ENTRY_PROMPT_ALLOWED_STATES = PLAN_CREATION_ENTRY_STATES


# ── Stub constants — kept so legacy imports don't fail before T5.4 cleanup ──
# These states are NO LONGER part of active FSM.
# T5.4: delete these stubs along with adaptation code in orchestrator / ai_plans.py.

ADAPTATION_SELECTION = "ADAPTATION_SELECTION"
ADAPTATION_PARAMS = "ADAPTATION_PARAMS"
ADAPTATION_CONFIRMATION = "ADAPTATION_CONFIRMATION"
ADAPTATION_FLOW_STATES: set[str] = set()           # removed from active FSM
ADAPTATION_FLOW_ALLOWED_TRANSITIONS: set = set()    # removed from active FSM
ADAPTATION_ENTRY_STATES: set[str] = set()           # removed
ADAPTATION_FLOW_ENTRY_STATES: set[str] = set()      # removed

PLAN_FLOW_STATES: set[str] = set()      # removed from active FSM
PLAN_FLOW_ENTRYPOINTS: set[str] = set() # removed

# Wildcard state families — matched via startswith(prefix + ":") in guards and DB LIKE.
# ONBOARDING:* covers all sub-states of the onboarding flow (ONBOARDING:START, etc.).
PREFIXED_STATES = {"ONBOARDING"}
