"""FSM state definitions.

T5.3 / T5.4: Adaptation flow and Plan flow tunnels removed.
Active FSM (9 state groups):

  IDLE_NEW → ONBOARDING:* → IDLE_ONBOARDED → ACTIVE
  ACTIVE ↔ ACTIVE_PAUSED
  ACTIVE / ACTIVE_PAUSED → IDLE_FINISHED / IDLE_PLAN_ABORTED / IDLE_DROPPED
  IDLE_FINISHED / IDLE_PLAN_ABORTED / IDLE_DROPPED → ACTIVE
  ACTIVE / ACTIVE_PAUSED ↔ SCHEDULE_ADJUSTMENT  (future: call tool — backlog)
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



# Wildcard state families — matched via startswith(prefix + ":") in guards and DB LIKE.
# ONBOARDING:* covers all sub-states of the onboarding flow (ONBOARDING:START, etc.).
PREFIXED_STATES = {"ONBOARDING"}

# ── Legacy stubs (T5.3) ───────────────────────────────────────────────────────
# PLAN_FLOW tunnel was removed in T5.3. orchestrator.py still imports these names
# in dead code guarded by LEGACY_PLAN_FLOW_ENABLED=False. Keep empty stubs until
# those imports/usages are cleaned up (backlog).
PLAN_FLOW_STATES: frozenset[str] = frozenset()
PLAN_FLOW_ENTRYPOINTS: frozenset[str] = frozenset()
